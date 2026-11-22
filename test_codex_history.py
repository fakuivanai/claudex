import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

import codex_history as history


def chat_id(number):
    return str(uuid.UUID(int=number))


class CodexHistoryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.database("state_5.sqlite")
        self.hist = self.database("thread_history_1.sqlite")
        self.state.executescript("""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, name TEXT, title TEXT, archived INT NOT NULL,
                updated_at_ms INT, recency_at_ms INT NOT NULL, originator TEXT, source TEXT,
                thread_source TEXT, agent_path TEXT, attached_thread_parent_id TEXT,
                history_mode TEXT NOT NULL
            );
            CREATE TABLE thread_spawn_edges (child_thread_id TEXT PRIMARY KEY);
        """)
        self.hist.executescript("""
            CREATE TABLE thread_turns (
                thread_id TEXT, turn_id TEXT, rollout_ordinal INT, status TEXT,
                started_at INT, completed_at INT, PRIMARY KEY (thread_id, turn_id)
            );
            CREATE INDEX idx_thread_turns_page ON thread_turns(thread_id, rollout_ordinal);
            CREATE TABLE thread_items (
                thread_id TEXT, turn_id TEXT, rollout_ordinal INT, item_type TEXT, item_json TEXT
            );
            CREATE INDEX idx_thread_items_by_turn_page
                ON thread_items(thread_id, turn_id, rollout_ordinal);
        """)

    def database(self, filename):
        db = sqlite3.connect(self.root / filename, isolation_level=None)
        self.addCleanup(db.close)
        return db

    def add_chat(self, number, name=None, title=None, archive=False, *,
                 origin="Codex Desktop", attached=None, mode="paginated"):
        self.state.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id(number), name, title, int(archive), number, number,
                origin, "vscode", "user", None, attached, mode,
            ),
        )

    def add_turn(self, chat_number, turn_number, items):
        target = chat_id(chat_number)
        turn = chat_id(turn_number)
        self.hist.execute(
            "INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?)",
            (target, turn, turn_number, "completed", 1, 2),
        )
        for ordinal, item in enumerate(items):
            self.hist.execute(
                "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?)",
                (target, turn, ordinal, item["type"], json.dumps(item)),
            )

    def test_listing_filters_origins_child_threads_and_archives(self):
        self.add_chat(1, name="Ático", title="Stored title")
        self.add_chat(2, title="Phone", origin="codex_chatgpt_android_remote")
        self.add_chat(3, name="Archive", archive=True)
        self.add_chat(4, name="VS Code", origin="codex_vscode")
        self.add_chat(5, name="Child")
        self.state.execute("INSERT INTO thread_spawn_edges VALUES (?)", (chat_id(5),))
        self.add_chat(6, name="Attached", attached=chat_id(1))

        first = history.list_threads(limit=1, root=self.root)
        self.assertEqual(first["origin"], "untrusted chat metadata")
        self.assertEqual([item["id"] for item in first["threads"]], [chat_id(2)])
        older = history.list_threads(limit=10, cursor=first["next_cursor"], root=self.root)
        self.assertEqual([item["id"] for item in older["threads"]], [chat_id(1)])
        self.assertEqual(older["threads"][0]["title"], "Ático")
        search = history.list_threads(query="ÁTICO", root=self.root)
        self.assertEqual([item["id"] for item in search["threads"]], [chat_id(1)])
        exact = history.list_threads(query=chat_id(2), root=self.root)
        self.assertEqual([item["id"] for item in exact["threads"]], [chat_id(2)])
        archives = history.list_threads(archived=True, root=self.root)
        self.assertEqual([item["id"] for item in archives["threads"]], [chat_id(3)])
        with self.assertRaisesRegex(history.HistoryError, "same query"):
            history.list_threads(archived=True, cursor=first["next_cursor"], root=self.root)

    def test_read_allowlist_discards_hidden_and_ordinary_tool_data(self):
        self.add_chat(1, title="Chat")
        peer = {
            "schema": "thread-bridge.peer-message/v1", "message_id": "peer-1",
            "sender": {"peer": "Claude fixture", "thread": "local_fixture", "secret": "hidden"},
            "untrusted_peer_content": "Literal peer data",
        }
        self.add_turn(1, 11, [
            {"type": "hookPrompt", "text": "hidden hook"},
            {"type": "reasoning", "summary": ["hidden reasoning"]},
            {"type": "userMessage", "content": [
                {"type": "text", "text": "Visible user text"},
                {"type": "localImage", "path": "/hidden/attachment"},
            ]},
            {"type": "commandExecution", "aggregatedOutput": "hidden command output"},
            {"type": "functionCallOutput", "namespace": "codex_app", "name": "peer_message",
             "output": "hidden app tool result"},
            {"type": "functionCallOutput", "namespace": "thread_bridge", "name": "peer_message",
             "output": json.dumps(peer)},
            {"type": "agentMessage", "phase": "final_answer", "text": "Visible reply",
             "memoryCitation": "hidden memory"},
        ])

        page = history.read_thread(chat_id(1), root=self.root)

        self.assertEqual(page["origin"], "untrusted chat history")
        visible = page["turns"][0]["messages"]
        self.assertEqual([entry["role"] for entry in visible], ["user", "peer", "assistant"])
        self.assertEqual(visible[1]["message_id"], "peer-1")
        self.assertEqual(visible[1]["sender"], {"peer": "Claude fixture", "thread": "local_fixture"})
        self.assertEqual(visible[2]["phase"], "final_answer")
        self.assertNotIn("hidden", json.dumps(page))

    def test_turn_pages_are_bound_to_exact_chat_and_explicit_archive_scope(self):
        self.add_chat(1, title="Chat", archive=True)
        self.add_chat(2, title="Legacy", mode="legacy")
        for number in (11, 12, 13):
            self.add_turn(1, number, [{"type": "agentMessage", "text": "Visible reply"}])

        for invalid in ("Chat", chat_id(2), chat_id(1)):
            with self.assertRaises(history.HistoryError):
                history.read_thread(invalid, root=self.root)

        recent = history.read_thread(chat_id(1), limit=2, include_archived=True, root=self.root)
        self.assertEqual([turn["id"] for turn in recent["turns"]], [chat_id(12), chat_id(13)])
        older = history.read_thread(
            chat_id(1), limit=2, cursor=recent["next_cursor"], include_archived=True, root=self.root,
        )
        self.assertEqual([turn["id"] for turn in older["turns"]], [chat_id(11)])
        self.assertIsNone(older["next_cursor"])
        with self.assertRaisesRegex(history.HistoryError, "same chat"):
            history.read_thread(chat_id(2), cursor=recent["next_cursor"], root=self.root)

    def test_preview_size_is_bounded(self):
        self.add_chat(1, title="Chat")
        messages = [
            {"type": "agentMessage", "text": "a" * (history.MAX_TEXT_CHARS + 1)}
            for _ in range(history.MAX_MESSAGES_PER_TURN + 1)
        ]
        self.add_turn(1, 11, messages)

        turn = history.read_thread(chat_id(1), root=self.root)["turns"][0]

        self.assertTrue(turn["messages_truncated"])
        self.assertEqual(len(turn["messages"]), history.MAX_MESSAGES_PER_TURN)
        self.assertTrue(all(message["truncated"] for message in turn["messages"]))
        self.assertTrue(all(len(message["text"]) == history.MAX_TEXT_CHARS for message in turn["messages"]))

    def test_read_connections_see_committed_wal_and_do_not_create_missing_databases(self):
        self.state.execute("PRAGMA journal_mode=WAL")
        self.add_chat(1, title="WAL visible")
        self.assertEqual(history.list_threads(root=self.root)["threads"][0]["title"], "WAL visible")
        missing = self.root / "does-not-exist"
        with self.assertRaises(history.HistoryError):
            history.list_threads(root=missing)
        self.assertFalse(missing.exists())
        with history._database(self.root, "state_5.sqlite") as db, self.assertRaises(sqlite3.OperationalError):
            db.execute("DELETE FROM threads")


if __name__ == "__main__":
    unittest.main()

from collections.abc import Sequence
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch
import uuid

from claudex.codex import history
from claudex.models import CodexChatId, Provider, Sender


def chat_id(number: int) -> CodexChatId:
    return CodexChatId(str(uuid.UUID(int=number)))


def fixture_items() -> tuple[str, list[dict[str, object]]]:
    path = Path(__file__).with_name("fixtures") / "turn.json"
    decoded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise AssertionError("Missing recorded turn")
    turn: dict[object, object] = decoded
    entries = turn.get("items")
    status = turn.get("status")
    if not isinstance(entries, list) or not isinstance(status, str):
        raise AssertionError("Missing recorded turn fields")
    result: list[dict[str, object]] = []
    for value in entries:
        if not isinstance(value, dict):
            raise AssertionError("Invalid recorded item")
        item = value.get("item")
        if not isinstance(item, dict) or item.get("type") != value.get("item_type"):
            raise AssertionError("Recorded item has the wrong kind")
        result.append(item)
    return status, result


class CodexHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory[str]()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.open_database("state_5.sqlite")
        self.history = self.open_database("thread_history_1.sqlite")
        self.state.executescript("""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, name TEXT, title TEXT, archived INT NOT NULL,
                updated_at_ms INT, recency_at_ms INT NOT NULL, originator TEXT, source TEXT,
                thread_source TEXT, agent_path TEXT, attached_thread_parent_id TEXT,
                history_mode TEXT NOT NULL
            );
            CREATE TABLE thread_spawn_edges (child_thread_id TEXT PRIMARY KEY);
        """)
        self.history.executescript("""
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

    def open_database(self, filename: str) -> sqlite3.Connection:
        database = sqlite3.connect(self.root / filename, isolation_level=None)
        self.addCleanup(database.close)
        return database

    def add_chat(
        self, number: int, *, name: str | None = None, title: str | None = None, archived: bool = False,
        origin: str = "Codex Desktop", attached: str | None = None, mode: str = "paginated",
        thread_source: str = "user",
    ) -> None:
        self.state.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id(number), name, title, int(archived), number, number,
                origin, "vscode", thread_source, None, attached, mode,
            ),
        )

    def add_turn(
        self, chat_number: int, turn_number: int, items: Sequence[dict[str, object]], *, status: str = "completed",
    ) -> None:
        target = chat_id(chat_number)
        turn = chat_id(turn_number)
        self.history.execute(
            "INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?)", (target, turn, turn_number, status, 1, 2),
        )
        for ordinal, item in enumerate(items):
            self.history.execute(
                "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?)", (target, turn, ordinal, item["type"], json.dumps(item)),
            )

    def test_listing_includes_only_top_level_desktop_chats(self) -> None:
        self.add_chat(1, name="Desktop")
        self.add_chat(2, title="Phone", origin="codex_chatgpt_android_remote")
        self.add_chat(3, name="VS Code", origin="codex_vscode")
        self.add_chat(4, name="Child")
        self.state.execute("INSERT INTO thread_spawn_edges VALUES (?)", (chat_id(4),))
        self.add_chat(5, name="Attached", attached=chat_id(1))
        page = history.list_chats(root=self.root)
        self.assertEqual([chat.id for chat in page.chats], [chat_id(2), chat_id(1)])

    def test_title_search_is_case_insensitive(self) -> None:
        self.add_chat(1, name="Ático", title="Stored title")
        search = history.list_chats(query="ÁTICO", root=self.root)
        self.assertEqual([chat.title for chat in search.chats], ["Ático"])

    def test_chat_listing_cursor_continues_in_recency_order(self) -> None:
        self.add_chat(1)
        self.add_chat(2)
        newest = history.list_chats(limit=1, root=self.root)
        older = history.list_chats(cursor=newest.next_cursor, root=self.root)
        self.assertEqual([chat.id for chat in older.chats], [chat_id(1)])

    def test_chat_listing_cursor_cannot_change_archive_scope(self) -> None:
        self.add_chat(1)
        self.add_chat(2)
        newest = history.list_chats(limit=1, root=self.root)
        with self.assertRaisesRegex(history.HistoryError, "same query"):
            history.list_chats(archived=True, cursor=newest.next_cursor, root=self.root)

    def test_chat_title_uses_most_recent_indexed_name(self) -> None:
        index = self.root / "session_index.jsonl"
        entries = [json.dumps({"id": chat_id(1), "thread_name": title}) for title in ("Original", "Renamed")]
        index.write_text("\n".join(["malformed", *entries]), encoding="utf-8")
        self.assertEqual(history.chat_title(chat_id(1), root=self.root), "Renamed")

    def test_running_turn_lookup_reads_only_the_latest_turn_for_the_chat(self) -> None:
        self.add_turn(1, 11, [], status="inProgress")
        self.add_turn(2, 12, [], status="inProgress")
        self.assertEqual(history.projected_running_turn_id(chat_id(1), root=self.root), chat_id(11))
        self.add_turn(1, 13, [], status="completed")
        self.assertIsNone(history.projected_running_turn_id(chat_id(1), root=self.root))

    def test_history_discards_hidden_and_ordinary_tool_data(self) -> None:
        self.add_chat(1, title="Chat")
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
            {"type": "agentMessage", "phase": "final_answer", "text": "Visible reply",
             "memoryCitation": "hidden memory"},
        ])
        page = history.read_chat(chat_id(1), root=self.root)
        self.assertEqual([message.preview.text for message in page.turns[0].messages], [
            "Visible user text", "Visible reply",
        ])
        self.assertEqual([message.to_wire()["author"] for message in page.turns[0].messages], [
            "unknown", "agent",
        ])

    def test_recorded_user_items_have_no_developer_provenance(self) -> None:
        path = Path(__file__).with_name("fixtures") / "unattributed_user_items.json"
        for number, record in enumerate(json.loads(path.read_text(encoding="utf-8")), 1):
            with self.subTest(recording=record["recording"]):
                self.add_chat(number, thread_source=record["thread_source"])
                self.add_turn(number, number + 10, [record["item"]])
                turn, = history.read_chat(chat_id(number), root=self.root).turns
                message, = turn.messages
                self.assertEqual(message.to_wire()["role"], "user")
                self.assertEqual(message.to_wire()["author"], "unknown")

    def test_developer_filter_keeps_chat_scope_and_does_not_read_items(self) -> None:
        self.add_chat(1, archived=True)
        self.add_turn(1, 11, [
            {"type": "userMessage", "author": "developer", "content": [
                {"type": "text", "text": "This body and its labels cannot establish authorship."},
            ]},
        ])
        with self.assertRaises(history.HistoryError):
            history.read_chat(chat_id(1), developer_only=True, root=self.root)
        with patch("claudex.codex.history.database", wraps=history.database) as database:
            page = history.read_chat(chat_id(1), include_archived=True, developer_only=True, root=self.root)
        self.assertEqual(page.turns, ())
        self.assertIsNone(page.next_cursor)
        self.assertEqual(database.call_args_list, [call(self.root, history.STATE_DATABASE)])

    def test_recorded_desktop_turn_preserves_peer_attribution(self) -> None:
        self.add_chat(1, title="Chat")
        status, items = fixture_items()
        self.add_turn(1, 11, items, status=status)
        page = history.read_chat(chat_id(1), root=self.root)
        peer = page.turns[0].messages[0]
        self.assertEqual(peer.to_wire()["author"], "agent")
        if not isinstance(peer, history.PeerOutput):
            self.fail("Expected attributed peer output from the recorded turn")
        self.assertEqual(
            peer.message.sender,
            Sender(Provider.CLAUDE, "local_00000000-0000-4000-8000-000000000001", "Claude fixture"),
        )

    def test_history_pages_list_each_turn_once(self) -> None:
        self.add_chat(1)
        for number in (11, 12, 13):
            self.add_turn(1, number, [{"type": "agentMessage", "text": "Visible reply"}])
        newest = history.read_chat(chat_id(1), limit=2, root=self.root)
        older = history.read_chat(chat_id(1), limit=2, cursor=newest.next_cursor, root=self.root)
        self.assertEqual(
            [turn.record.id for turn in (*newest.turns, *older.turns)], [chat_id(12), chat_id(13), chat_id(11)],
        )

    def test_history_requires_explicit_archive_scope(self) -> None:
        self.add_chat(1, archived=True)
        with self.assertRaises(history.HistoryError):
            history.read_chat(chat_id(1), root=self.root)
        selected = history.read_chat(chat_id(1), include_archived=True, root=self.root)
        self.assertTrue(selected.chat.archived)

    def test_history_rejects_legacy_projection(self) -> None:
        self.add_chat(1, mode="legacy")
        with self.assertRaisesRegex(history.HistoryError, "paginated"):
            history.read_chat(chat_id(1), root=self.root)

    def test_history_message_preview_is_bounded(self) -> None:
        self.add_chat(1)
        self.add_turn(1, 11, [{"type": "agentMessage", "text": "a" * (history.MAX_TEXT_CHARS + 1)}])
        message, = history.read_chat(chat_id(1), root=self.root).turns[0].messages
        self.assertEqual(message.preview, history.VisibleText("a" * history.MAX_TEXT_CHARS, True))

    def test_turn_cap_keeps_original_user_message_and_newest_replies(self) -> None:
        self.add_chat(1)
        items: list[dict[str, object]] = [
            {"type": "userMessage", "content": [{"type": "text", "text": "Original request"}]},
        ]
        items.extend({"type": "agentMessage", "text": f"Reply {number}"} for number in range(10))
        self.add_turn(1, 11, items)
        turn, = history.read_chat(chat_id(1), root=self.root).turns
        self.assertEqual(
            [message.preview.text for message in turn.messages],
            ["Original request", *(f"Reply {number}" for number in range(3, 10))],
        )
        self.assertTrue(turn.over_message_cap)
        self.assertEqual(turn.skipped_records, 0)

    def test_skipped_records_do_not_count_as_visible_overflow(self) -> None:
        self.add_chat(1)
        self.add_turn(1, 11, [
            {"type": "agentMessage", "text": "Visible"},
            {"type": "agentMessage", "text": 7},
            {"type": "reasoning", "summary": ["hidden"]},
        ])
        turn, = history.read_chat(chat_id(1), root=self.root).turns
        self.assertEqual([message.preview.text for message in turn.messages], ["Visible"])
        self.assertFalse(turn.over_message_cap)
        self.assertEqual(turn.skipped_records, 1)

    def test_read_only_history_sees_committed_wal(self) -> None:
        self.state.execute("PRAGMA journal_mode=WAL")
        self.add_chat(1, title="WAL visible")
        self.assertEqual(history.list_chats(root=self.root).chats[0].title, "WAL visible")


if __name__ == "__main__":
    unittest.main()

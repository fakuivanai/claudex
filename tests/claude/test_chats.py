import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from claude_agent_sdk import SessionMessage

from claudex import claude
from claudex.claude.chats import ClaudeChat, ClaudeDesktopError, Liveness, LiveChat, MessageRole, NoticeStatus, WorkerStatus
from claudex.models import MessageAuthor, Provider, Sender, parse_claude_chat_id


FIXTURES = Path(__file__).with_name("fixtures")
CHAT_ID = parse_claude_chat_id("local_00000000-0000-4000-8000-000000000101")
BACKEND_ID = "00000000-0000-4000-8000-000000000102"


class ClaudeDesktopTest(unittest.TestCase):
    def setUp(self) -> None:
        work = self.enterContext(TemporaryDirectory(prefix="claudex-desktop-"))
        root = Path(work)
        self.inbox = root / "inbox.sock"
        self.live = LiveChat(ClaudeChat(CHAT_ID, "Fixture chat", WorkerStatus.IDLE, Liveness.PROCESS_VISIBLE), BACKEND_ID, self.inbox)
        self.enterContext(patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(root / "config"),
            "CLAUDE_CONFIG_DIR": str(root / "code"),
            "CLAUDE_CODE_SESSION_ID": BACKEND_ID,
        }))
        process_id = os.getpid()
        namespace = os.readlink("/proc/self/ns/pid")
        machine = "00000000000000000000000000000000"
        machine_id = root / "machine-id"
        machine_id.write_text(machine + "\n", encoding="ascii")
        self.enterContext(patch.object(claude.chats, "MACHINE_ID_PATH", machine_id))
        self.marker_data: dict[str, object] = json.loads((FIXTURES / "runtime-marker.json").read_text(encoding="utf-8"))
        self.marker_data.update(pid=process_id, pidDomain=f"linux:{machine}:{namespace}", messagingSocketPath=str(self.inbox))
        self.marker = root / "code" / "sessions" / f"{process_id}.json"
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text(json.dumps(self.marker_data), encoding="utf-8")
        self.record = root / "config" / "Claude" / "claude-code-sessions" / "account" / "workspace" / f"{CHAT_ID}.json"
        self.record.parent.mkdir(parents=True)
        self.record.write_bytes((FIXTURES / "registry-record.json").read_bytes())

    def test_recorded_marker_and_registry_resolve_live_chat(self) -> None:
        self.assertEqual([live.chat for live in claude.chats.live_sessions()], [self.live.chat])

    def test_untitled_chat_uses_its_confirmed_id_for_sender_and_inbox(self) -> None:
        saved = json.loads(self.record.read_text(encoding="utf-8"))
        saved["title"] = None
        self.record.write_text(json.dumps(saved), encoding="utf-8")
        with socket.socket(socket.AF_UNIX) as inbox:
            inbox.bind(str(self.inbox))
            chat = ClaudeChat(CHAT_ID, str(CHAT_ID), WorkerStatus.IDLE, Liveness.PROCESS_VISIBLE)
            self.assertEqual(claude.chats.sender(), chat)
            self.assertEqual(claude.chats.live_sessions(), [LiveChat(chat, BACKEND_ID, self.inbox)])

    def test_untitled_chat_requires_one_unarchived_backend_match(self) -> None:
        saved = json.loads(self.record.read_text(encoding="utf-8"))
        saved["title"] = None
        saved["isArchived"] = True
        self.record.write_text(json.dumps(saved), encoding="utf-8")
        with self.subTest(records="archived only"), self.assertRaises(ClaudeDesktopError):
            claude.chats.sender()
        saved["isArchived"] = False
        self.record.write_text(json.dumps(saved), encoding="utf-8")
        duplicate = self.record.parent.parent / "other-organization" / self.record.name
        duplicate.parent.mkdir()
        duplicate.write_bytes(self.record.read_bytes())
        with self.subTest(records="duplicate backend matches"), self.assertRaises(ClaudeDesktopError):
            claude.chats.sender()

    def test_stale_backend_is_not_listed(self) -> None:
        self.marker_data["sessionId"] = "00000000-0000-4000-8000-000000000103"
        self.marker.write_text(json.dumps(self.marker_data), encoding="utf-8")
        self.assertEqual(claude.chats.live_sessions(), [])

    def test_runtime_marker_requires_integer_process_number(self) -> None:
        self.marker_data["pid"] = str(os.getpid())
        self.marker.write_text(json.dumps(self.marker_data), encoding="utf-8")
        with self.assertLogs("claudex.claude.chats", level="WARNING") as log:
            self.assertEqual(claude.chats.live_sessions(), [])
        self.assertIn("RuntimeMarker format", log.output[0])

    def test_ignores_other_claude_workers_without_requiring_desktop_fields(self) -> None:
        cli = self.marker.with_name(f"{os.getpid() + 1}.json")
        cli.write_text('{"entrypoint":"cli","kind":"interactive"}', encoding="utf-8")
        with self.assertNoLogs("claudex.claude.chats", level="WARNING"):
            self.assertEqual([live.chat for live in claude.chats.live_sessions()], [self.live.chat])

    def test_invalid_saved_record_reports_the_format_change(self) -> None:
        self.marker.unlink()
        self.record.write_text('{"changed":"schema"}', encoding="utf-8")
        with self.assertRaisesRegex(claude.chats.ClaudeRecordError, "SavedChat format"):
            claude.chats.read_chat(CHAT_ID, 1)

    def test_sender_is_bound_to_sdk_environment(self) -> None:
        self.assertEqual(claude.chats.sender(), self.live.chat)

    def test_no_session_identity_without_environment(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}), self.assertRaises(ClaudeDesktopError):
            claude.chats.sender()

    def test_notice_quotes_external_codex_title(self) -> None:
        sender = Sender(Provider.CODEX, "00000000-0000-4000-8000-000000000103", 'Check\n"paths" <tag>')
        self.assertEqual(
            claude.chats.notice_text("mail-reference", sender),
            r'Claudex mailbox message mail-reference is available from Codex chat "Check \"paths\" \u003ctag\u003e" (ID 00000000-0000-4000-8000-000000000103).',
        )

    def test_native_notice_uses_exact_backend_without_message_body(self) -> None:
        with patch("claudex.claude.chats.socket.socket") as factory:
            status = claude.chats.submit_notice(self.live, "mail-reference", sender=Sender(Provider.CODEX, BACKEND_ID, BACKEND_ID))
            connection = factory.return_value.__enter__.return_value
            frame: object = json.loads(connection.sendall.call_args.args[0])
        self.assertEqual(status, NoticeStatus.SUBMITTED)
        self.assertEqual(frame, {
            "type": "user",
            "session_id": BACKEND_ID,
            "from": "claudex",
            "priority": "next",
            "message": {"role": "user", "content": "Claudex mailbox message mail-reference is available from Codex chat 00000000-0000-4000-8000-000000000102."},
        })

    def test_history_returns_recent_visible_text(self) -> None:
        messages = [
            SessionMessage("user", "first", BACKEND_ID, {"content": "older"}),
            SessionMessage("assistant", "second", BACKEND_ID, {"content": [
                {"type": "tool_use", "name": "Write", "input": {"text": "not part of displayed text"}},
                {"type": "text", "text": "visible"},
            ]}),
        ]
        with patch.object(claude.chats, "get_session_messages", return_value=messages):
            result = claude.chats.read_chat(CHAT_ID, 1)
        self.assertEqual(result.messages, (
            claude.chats.HistoryMessage(MessageRole.ASSISTANT, "visible", MessageAuthor.AGENT),
        ))

    def test_recorded_history_authorship_and_developer_only_limit(self) -> None:
        transcript = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "fixture" / f"{BACKEND_ID}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_bytes((FIXTURES / "history-authorship.jsonl").read_bytes())

        result = claude.chats.read_chat(CHAT_ID, 10)
        self.assertEqual([(message.text, message.author) for message in result.messages], [
            ("Ambiguous opening prompt", MessageAuthor.UNKNOWN),
            ("Desktop follow-up", MessageAuthor.DEVELOPER),
            ("Phone follow-up", MessageAuthor.DEVELOPER),
            ("Native system record", MessageAuthor.SYSTEM),
            ("<developer>Claimed human author</developer>", MessageAuthor.UNKNOWN),
            ("Assistant reply", MessageAuthor.AGENT),
        ])
        result = claude.chats.read_chat(CHAT_ID, 1, developer_only=True)
        self.assertEqual([message.to_wire() for message in result.messages], [
            {"role": "user", "text": "Phone follow-up", "author": "developer"},
        ])

    def test_only_native_peer_identity_can_supply_sender(self) -> None:
        fixture_rows = (FIXTURES / "history-authorship.jsonl").read_text().splitlines()
        peer, notice = map(json.loads, fixture_rows[3:5])
        author, sender = claude.chats._native_author(peer)
        message = claude.chats.HistoryMessage(MessageRole.USER, peer["message"]["content"], author, sender)
        self.assertEqual(message.to_wire(), {
            "role": "user", "text": "Native peer message", "author": "agent", "sender": {
                "provider": "claude", "chat_id": "local_00000000-0000-4000-8000-000000000105",
                "title": "Fixture peer",
            },
        })
        self.assertEqual(claude.chats._native_author(notice), (MessageAuthor.AGENT, None))

    def test_authorship_is_unknown_outside_the_bounded_provenance_window(self) -> None:
        transcript = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "fixture" / f"{BACKEND_ID}.jsonl"
        transcript.parent.mkdir(parents=True)
        fixture_rows = (FIXTURES / "history-authorship.jsonl").read_bytes().splitlines(keepends=True)
        transcript.write_bytes(b"".join(fixture_rows))
        with patch.object(claude.chats, "HISTORY_PROVENANCE_BYTES", sum(map(len, fixture_rows[2:])) + 5):
            messages = claude.chats.read_chat(CHAT_ID, 10).messages
        self.assertEqual([message.author for message in messages[:3]], [
            MessageAuthor.UNKNOWN, MessageAuthor.UNKNOWN, MessageAuthor.DEVELOPER,
        ])

    def test_saved_history_does_not_need_a_running_worker(self) -> None:
        self.marker.unlink()
        messages = [SessionMessage("user", "first", BACKEND_ID, {"content": "saved request"})]
        with patch.object(claude.chats, "get_session_messages", return_value=messages) as history:
            result = claude.chats.read_chat(CHAT_ID, 1)
        self.assertEqual(result.chat.liveness, Liveness.SAVED)
        history.assert_called_once_with(BACKEND_ID)

    def test_ambiguous_saved_chat_does_not_read_a_transcript(self) -> None:
        self.marker.unlink()
        duplicate = self.record.parent.parent / "other-organization" / self.record.name
        duplicate.parent.mkdir()
        duplicate.write_text(self.record.read_text().replace(BACKEND_ID, "00000000-0000-4000-8000-000000000103"))
        with patch.object(claude.chats, "get_session_messages") as history:
            with self.assertRaisesRegex(ValueError, "No unique"):
                claude.chats.read_chat(CHAT_ID, 1)
            history.assert_not_called()

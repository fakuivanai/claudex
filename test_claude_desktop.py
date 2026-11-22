import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from claude_agent_sdk import SessionMessage

import claude_desktop
from claude_desktop import ClaudeChat, ClaudeDesktopError, CodexNoticeSender, Liveness, LiveChat, MessageRole, NoticeStatus


FIXTURES = Path(__file__).parent / "fixtures" / "claude_desktop_linux"
CHAT_ID = claude_desktop.parse_chat_identifier("local_00000000-0000-4000-8000-000000000101")
BACKEND_ID = "00000000-0000-4000-8000-000000000102"


class ClaudeDesktopTest(unittest.TestCase):
    def setUp(self) -> None:
        work = self.enterContext(TemporaryDirectory(prefix="claudex-desktop-"))
        root = Path(work)
        self.inbox = root / "inbox.sock"
        self.live = LiveChat(ClaudeChat(CHAT_ID, "Fixture chat", "idle", Liveness.PROCESS_VISIBLE), BACKEND_ID, self.inbox)
        self.enterContext(patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(root / "config"),
            "CLAUDE_CONFIG_DIR": str(root / "code"),
            "CLAUDE_CODE_SESSION_ID": BACKEND_ID,
        }))
        process_id = os.getpid()
        namespace = os.readlink("/proc/self/ns/pid")
        machine = Path("/etc/machine-id").read_text(encoding="ascii").strip()
        self.marker_data: dict[str, object] = json.loads((FIXTURES / "runtime-marker.json").read_text(encoding="utf-8"))
        self.marker_data.update(pid=process_id, pidDomain=f"linux:{machine}:{namespace}", messagingSocketPath=str(self.inbox))
        self.marker = root / "code" / "sessions" / f"{process_id}.json"
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text(json.dumps(self.marker_data), encoding="utf-8")
        self.record = root / "config" / "Claude" / "claude-code-sessions" / "account" / "workspace" / f"{CHAT_ID}.json"
        self.record.parent.mkdir(parents=True)
        self.record.write_bytes((FIXTURES / "registry-record.json").read_bytes())

    def test_recorded_marker_and_registry_resolve_live_chat(self) -> None:
        self.assertEqual(claude_desktop.list_chats(), [self.live.chat])

    def test_stale_backend_is_not_listed(self) -> None:
        self.marker_data["sessionId"] = "00000000-0000-4000-8000-000000000103"
        self.marker.write_text(json.dumps(self.marker_data), encoding="utf-8")
        self.assertEqual(claude_desktop.list_chats(), [])

    def test_runtime_marker_requires_integer_process_number(self) -> None:
        self.marker_data["pid"] = str(os.getpid())
        self.marker.write_text(json.dumps(self.marker_data), encoding="utf-8")
        self.assertEqual(claude_desktop.list_chats(), [])

    def test_sender_is_bound_to_sdk_environment(self) -> None:
        self.assertEqual(claude_desktop.sender(), self.live.chat)

    def test_no_session_identity_without_environment(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}), self.assertRaises(ClaudeDesktopError):
            claude_desktop.sender()

    def test_notice_quotes_external_codex_title(self) -> None:
        sender = CodexNoticeSender("00000000-0000-4000-8000-000000000103", 'Check\n"paths" <tag>')
        self.assertEqual(
            claude_desktop.notice_text("mail-reference", sender),
            r'Claudex mailbox message mail-reference is available from Codex chat "Check \"paths\" \u003ctag\u003e" (ID 00000000-0000-4000-8000-000000000103).',
        )

    def test_native_notice_uses_exact_backend_without_message_body(self) -> None:
        with patch("claude_desktop.socket.socket") as factory:
            status = claude_desktop.submit_notice(self.live, "mail-reference", sender=None)
            connection = factory.return_value.__enter__.return_value
            frame: object = json.loads(connection.sendall.call_args.args[0])
        self.assertEqual(status, NoticeStatus.SUBMITTED)
        self.assertEqual(frame, {
            "type": "user",
            "session_id": BACKEND_ID,
            "from": "thread-bridge",
            "priority": "next",
            "message": {"role": "user", "content": "Claudex mailbox message mail-reference is available."},
        })

    def test_history_returns_recent_visible_text(self) -> None:
        messages = [
            SessionMessage("user", "first", BACKEND_ID, {"content": "older"}),
            SessionMessage("assistant", "second", BACKEND_ID, {"content": [
                {"type": "tool_use", "name": "Write", "input": {"text": "not part of displayed text"}},
                {"type": "text", "text": "visible"},
            ]}),
        ]
        with patch.object(claude_desktop, "get_session_messages", return_value=messages):
            result = claude_desktop.read_chat(CHAT_ID, 1)
        self.assertEqual(result.messages, (claude_desktop.HistoryMessage(MessageRole.ASSISTANT, "visible"),))

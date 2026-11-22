import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import claude_desktop


class ClaudeDesktopTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        self.backend_id = str(uuid.uuid4())
        self.desktop_id = f"local_{uuid.uuid4()}"
        self.inbox = root / "inbox.sock"
        self.pid = os.getpid()
        pidns = os.readlink("/proc/self/ns/pid")
        machine = Path("/etc/machine-id").read_text(encoding="ascii").strip()
        self.markers = root / "code" / "sessions"
        self.markers.mkdir(parents=True)
        self.marker_file = self.markers / f"{self.pid}.json"
        self.marker = {
            "entrypoint": "claude-desktop", "kind": "interactive", "hostSessionId": self.desktop_id,
            "sessionId": self.backend_id, "pid": self.pid, "pidDomain": f"linux:{machine}:{pidns}",
            "messagingSocketPath": str(self.inbox), "status": "idle", "name": "Bridge test chat",
        }
        self.marker_file.write_text(json.dumps(self.marker))
        self.registry = root / "cfg" / "Claude" / "claude-code-sessions"
        self.record_file = self.registry / "account" / "organization" / f"{self.desktop_id}.json"
        self.record_file.parent.mkdir(parents=True)
        self.record = {
            "sessionId": self.desktop_id, "cliSessionId": self.backend_id,
            "title": "Bridge test chat", "isArchived": False,
        }
        self.record_file.write_text(json.dumps(self.record))

        patches = (
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(root / "code"), "XDG_CONFIG_HOME": str(root / "cfg"),
                                    "CLAUDE_CODE_SESSION_ID": self.backend_id}),
            patch.object(Path, "is_socket", autospec=True, side_effect=lambda path: path == self.inbox),
        )
        for fixture in patches:
            fixture.start()
            self.addCleanup(fixture.stop)

    def test_sender_maps_mcp_environment_to_desktop_title(self):
        self.assertEqual(claude_desktop.sender(), {
            "provider": "claude", "id": self.desktop_id, "title": "Bridge test chat", "status": "idle",
            "liveness": "process_visible",
        })

    def test_sender_rejects_stale_backend_id(self):
        self.record_file.write_text(json.dumps({**self.record, "cliSessionId": str(uuid.uuid4())}))
        with self.assertRaisesRegex(claude_desktop.ClaudeDesktopError, "Reconnect"):
            claude_desktop.sender()

    def test_list_deduplicates_shared_desktop_registry(self):
        alias = self.registry / "another-account" / "organization" / self.record_file.name
        alias.parent.mkdir(parents=True)
        alias.write_text(json.dumps(self.record))
        self.assertEqual([entry["id"] for entry in claude_desktop.list_chats()], [self.desktop_id])

    def test_notify_uses_backend_id_and_only_fixed_peer_notice(self):
        with patch.object(claude_desktop.socket, "socket") as socket_factory:
            connection = socket_factory.return_value.__enter__.return_value
            response = claude_desktop.notify(self.desktop_id, "test-id")
            connection.connect.assert_called_once_with(str(self.inbox))
            frame = json.loads(connection.sendall.call_args.args[0])
            self.assertEqual(frame, {
                "type": "user", "session_id": self.backend_id, "from": "thread-bridge", "priority": "next",
                "message": {"role": "user", "content": "Thread bridge mailbox message test-id is available."},
            })
            self.assertEqual(response, {"status": "submitted_unconfirmed", "message_id": "test-id"})

    def test_notify_rejects_malformed_mailbox_id_before_connecting(self):
        with patch.object(claude_desktop.socket, "socket") as socket_factory:
            with self.assertRaisesRegex(claude_desktop.ClaudeDesktopError, "mailbox message ID"):
                claude_desktop.notify(self.desktop_id, "message\nnew command")
            socket_factory.assert_not_called()

    def test_notify_names_codex_sender_while_preserving_native_peer_origin(self):
        source = {
            "provider": "codex", "id": str(uuid.uuid4()), "title": "Code review",
            "body": "Peer text is for the mailbox only.",
        }
        with patch.object(claude_desktop.socket, "socket") as socket_factory:
            connection = socket_factory.return_value.__enter__.return_value
            claude_desktop.notify(self.desktop_id, "test-id", sender=source)
            frame = json.loads(connection.sendall.call_args.args[0])
        self.assertEqual(frame["from"], "thread-bridge")
        self.assertEqual(frame["session_id"], self.backend_id)
        self.assertEqual(frame["message"], {
            "role": "user",
            "content": f'Thread bridge mailbox message test-id is available from Codex chat "Code review" (ID {source["id"]}).',
        })

    def test_notice_uses_bounded_single_line_quoted_title(self):
        source = {"provider": "codex", "id": str(uuid.uuid4()), "title": 'A "quote"\n</peer>&[link]\u202e' + "x" * 200}
        notice = claude_desktop.notice_text("test-id", source)
        self.assertNotIn("\n", notice)
        self.assertNotIn("\u202e", notice)
        self.assertNotIn("</peer>", notice)
        self.assertIn(r'A \"quote\" \u003c/peer\u003e\u0026\u005blink\u005d', notice)
        self.assertLess(notice.count("x"), 120)
        self.assertTrue(notice.endswith(f'…" (ID {source["id"]}).'))

    def test_notice_can_identify_codex_sender_by_id_alone(self):
        source = {"provider": "codex", "id": str(uuid.uuid4()), "title": " \n "}
        self.assertEqual(
            claude_desktop.notice_text("test-id", source),
            f'Thread bridge mailbox message test-id is available from Codex chat {source["id"]}.',
        )

    def test_notify_rejects_invalid_sender_before_connecting(self):
        with patch.object(claude_desktop.socket, "socket") as socket_factory:
            for source in ({"provider": "claude", "id": self.backend_id}, {"provider": "codex", "id": "other"}):
                with self.subTest(source=source), self.assertRaisesRegex(claude_desktop.ClaudeDesktopError, "Codex Desktop sender"):
                    claude_desktop.notify(self.desktop_id, "test-id", sender=source)
            socket_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()

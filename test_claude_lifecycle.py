from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import claude_desktop
from claude_desktop import ClaudeChat, LiveChat, Liveness, NoticeStatus, parse_chat_identifier
import claude_lifecycle


TARGET = parse_chat_identifier("local_00000000-0000-4000-8000-000000000101")
MARKER = LiveChat(
    ClaudeChat(TARGET, "Fixture chat", "idle", Liveness.INBOX_PRESENT),
    "00000000-0000-4000-8000-000000000102",
    Path("/tmp/claudex-fixture/inbox.sock"),
)


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.launcher = self.enterContext(patch.object(claude_lifecycle, "launch"))
        self.lookup = self.enterContext(patch.object(claude_lifecycle, "wait_for_inbox"))
        self.validate = self.enterContext(patch.object(claude_lifecycle, "validate_saved"))
        self.notice = self.enterContext(patch.object(claude_desktop, "submit_notice"))

    def test_live_inbox_skips_launch(self) -> None:
        self.lookup.return_value = MARKER
        claude_lifecycle.wake(TARGET)
        self.launcher.assert_not_called()

    def test_cold_chat_passes_exact_saved_identifier(self) -> None:
        self.lookup.side_effect = [None, MARKER]
        claude_lifecycle.wake(TARGET)
        self.launcher.assert_called_once_with(f"claude://code/continue?session={TARGET}")

    def test_cold_chat_is_validated_before_launch(self) -> None:
        self.lookup.return_value = None
        self.validate.side_effect = ValueError("Unknown saved chat")
        with self.assertRaises(ValueError):
            claude_lifecycle.wake(TARGET)
        self.launcher.assert_not_called()

    def test_reopen_without_worker_reports_waiting(self) -> None:
        self.lookup.return_value = None
        self.assertEqual(claude_lifecycle.wake(TARGET).status, claude_lifecycle.WakeStatus.WAITING_FOR_INBOX)

    def test_notify_uses_the_worker_returned_after_reopen(self) -> None:
        self.lookup.side_effect = [None, MARKER]
        self.notice.return_value = NoticeStatus.SUBMITTED
        claude_lifecycle.notify(TARGET, "message")
        self.notice.assert_called_once_with(MARKER, "message", None)

    def test_notify_without_worker_leaves_message_stored(self) -> None:
        self.lookup.return_value = None
        self.assertIsInstance(claude_lifecycle.notify(TARGET, "message"), claude_lifecycle.StoredNotice)

    def test_draft_encodes_prompt_as_data(self) -> None:
        prompt = "Check A & B? folder=/elsewhere"
        with TemporaryDirectory(prefix="claudex test ") as folder:
            url = urlsplit(claude_lifecycle.draft_url(prompt, folder))
        self.assertEqual(parse_qs(url.query), {"q": [prompt], "folder": [folder]})

    def test_open_draft_uses_native_code_draft_path(self) -> None:
        claude_lifecycle.open_draft("Show the current project")
        url = urlsplit(self.launcher.call_args.args[0])
        self.assertEqual((url.scheme, url.netloc, url.path), ("claude", "code", "/new"))

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import claude_lifecycle
import claude_remote


class LifecycleTest(unittest.TestCase):
    def test_remote_log_does_not_attribute_another_chats_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "main.log"
            log.write_text('Remote control enabled: "old"\nbridge_state: "connected"\n')
            cursor = claude_remote.log_position(log)
            with log.open("a") as handle:
                handle.write(
                    'Enabling remote control for session local_a\n'
                    'Enabling remote control for session local_b\n'
                    'Remote control enabled: "redacted"\nbridge_state: "connected"\n'
                )
            self.assertFalse(claude_remote.connection_seen(log, cursor, "local_a"))
            self.assertTrue(claude_remote.connection_seen(log, cursor, "local_b"))

    @patch("claude_lifecycle.launch")
    def test_draft_encodes_the_prompt_as_data(self, launcher):
        prompt = "Check A & B? folder=/elsewhere"
        self.assertEqual(claude_lifecycle.open_draft(prompt)["status"], "draft_requested")
        url = urlsplit(launcher.call_args.args[0])
        self.assertEqual((url.scheme, url.netloc, url.path), ("claude", "code", "/new"))
        self.assertEqual(parse_qs(url.query), {"q": [prompt]})

    @patch("claude_lifecycle.claude_desktop.notify", return_value={"status": "submitted_unconfirmed"})
    @patch("claude_lifecycle.launch")
    @patch("claude_lifecycle.wait_for_box", return_value={"id": "target"})
    def test_live_inbox_uses_native_notice_without_launching(self, lookup, launcher, notice):
        self.assertFalse(claude_lifecycle.notify("target", "message")["opened_saved_chat"])
        launcher.assert_not_called()
        notice.assert_called_once_with("target", "message", sender=None)

    @patch("claude_lifecycle.claude_desktop.notify", return_value={"status": "submitted_unconfirmed"})
    @patch("claude_lifecycle.launch")
    @patch("claude_lifecycle.validate_saved")
    @patch("claude_lifecycle.wait_for_box", side_effect=[None, {"id": "target"}])
    def test_cold_chat_launches_exact_id_before_notice(self, lookup, validate, launcher, notice):
        self.assertTrue(claude_lifecycle.notify("target", "message")["opened_saved_chat"])
        validate.assert_called_once_with("target")
        launcher.assert_called_once_with("claude://code/continue?session=target")
        notice.assert_called_once_with("target", "message", sender=None)

    @patch("claude_lifecycle.claude_desktop.notify")
    @patch("claude_lifecycle.launch")
    @patch("claude_lifecycle.validate_saved")
    @patch("claude_lifecycle.wait_for_box", return_value=None)
    def test_no_inbox_keeps_mail_stored(self, lookup, validate, launcher, notice):
        self.assertEqual(claude_lifecycle.notify("target", "message")["status"], "stored")
        notice.assert_not_called()

    @patch("claude_lifecycle.launch")
    @patch("claude_lifecycle.validate_saved")
    @patch("claude_lifecycle.wait_for_box", return_value=None)
    def test_reopen_without_a_worker_reports_waiting(self, lookup, validate, launcher):
        self.assertEqual(claude_lifecycle.wake("target"), {
            "status": "waiting_for_inbox",
            "thread_id": "target",
        })


if __name__ == "__main__":
    unittest.main()

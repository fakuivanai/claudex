import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcp.types import RequestParams

import desktop_bridge


CLAUDE = {"id": "local-claude-test", "provider": "claude", "title": "Claude test"}
CODEX_ID = "22222222-2222-4222-8222-222222222222"
CODEX = {"id": CODEX_ID, "provider": "codex", "title": "Codex test"}


class DesktopBridgeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.bridge = desktop_bridge.Bridge("claude", Path(temporary.name) / "mail.sqlite", CODEX_ID)

    def test_request_identity_comes_from_the_calling_chat(self):
        meta = RequestParams.Meta.model_validate({
            "threadId": CODEX_ID,
            "x-codex-turn-metadata": {"thread_id": CODEX_ID},
        })
        context = SimpleNamespace(request_context=SimpleNamespace(meta=meta))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "33333333-3333-4333-8333-333333333333"}):
            source = desktop_bridge.codex_sender(context)
            self.assertEqual(source["id"], CODEX_ID)
            self.assertEqual(source["identity_source"], "request_metadata")
            context.request_context.meta = None
            with self.assertRaisesRegex(ValueError, "did not provide"):
                desktop_bridge.codex_sender(context)

    def test_sender_title_and_id_pass_to_native_codex_delivery(self):
        with patch("desktop_bridge.codex_desktop.deliver", return_value={"state": "submitted"}) as deliver:
            result = self.bridge.send(CLAUDE, CODEX_ID, "untrusted sample")
        target, message = deliver.call_args.args
        self.assertEqual(target, CODEX_ID)
        self.assertEqual(message["sender"]["peer"], CLAUDE["title"])
        self.assertEqual(message["sender"]["thread"], CLAUDE["id"])
        self.assertEqual(result["delivery"]["state"], "submitted")

    def test_other_codex_target_cannot_be_used(self):
        with patch("desktop_bridge.codex_desktop.deliver") as deliver:
            with self.assertRaises(ValueError):
                self.bridge.send(CLAUDE, "unexpected", "test")
        deliver.assert_not_called()

    def test_reading_an_uninitialized_inbox_does_not_create_state(self):
        self.assertEqual(self.bridge.inbox(CLAUDE, 2), {"messages": [], "next_cursor": 2})
        self.assertFalse(self.bridge.state.exists())

    def test_notice_only_gets_message_id_and_mail_survives_failure(self):
        with (
            patch("desktop_bridge.claude_lifecycle.validate_saved"),
            patch("desktop_bridge.claude_lifecycle.notify", side_effect=OSError("offline")) as notify,
        ):
            receipt = self.bridge.send(CODEX, CLAUDE["id"], "peer body")
        notify.assert_called_once_with(CLAUDE["id"], receipt["message_id"], sender=CODEX)
        self.assertEqual(receipt["delivery"]["status"], "stored")
        self.assertEqual(self.bridge.inbox(CLAUDE, 0)["messages"][0]["body"], "peer body")
        self.assertEqual(self.bridge.inbox(CODEX, 0)["messages"], [])


if __name__ == "__main__":
    unittest.main()

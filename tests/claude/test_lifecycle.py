import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import create_autospec, patch
from urllib.parse import parse_qs, urlsplit

from claudex import claude
from claudex.claude.chats import ClaudeChat, LiveChat, Liveness, NoticeStatus, WorkerStatus
from claudex.claude.control import DesktopControl, DesktopMutation, DesktopChat, Success, Unavailable
from claudex.models import Provider, Sender, parse_claude_chat_id


TARGET = parse_claude_chat_id("local_00000000-0000-4000-8000-000000000101")
MARKER = LiveChat(
    ClaudeChat(TARGET, "Fixture chat", WorkerStatus.IDLE, Liveness.INBOX_PRESENT),
    "00000000-0000-4000-8000-000000000102",
    Path("/tmp/claudex-fixture/inbox.sock"),
)


SENDER = Sender(Provider.CODEX, "00000000-0000-4000-8000-000000000103", "Fixture Codex")


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.launcher = self.enterContext(patch.object(claude.lifecycle, "launch"))
        self.lookup = self.enterContext(patch.object(claude.lifecycle, "wait_for_inbox"))
        self.control = create_autospec(DesktopControl, instance=True)
        self.control.warm_chat.return_value = Success(result=DesktopMutation(
            chat=DesktopChat(
                chat_id=TARGET, title="Fixture chat", running=False, archived=False, warm=True,
                remote_control="off", remote_control_saved=None, model=None, effort=None,
                fast_mode=None, permission_mode=None,
            ),
        ))
        self.notice = self.enterContext(patch.object(claude.chats, "submit_notice"))

    def notify(self) -> claude.lifecycle.NoticeResult:
        return claude.lifecycle.notify(TARGET, "message", sender=SENDER, control=self.control)

    def test_live_inbox_skips_launch_and_warm(self) -> None:
        self.lookup.return_value = MARKER
        self.notice.return_value = NoticeStatus.SUBMITTED
        self.assertEqual(self.notify(), claude.lifecycle.AttemptedNotice(NoticeStatus.SUBMITTED, False))
        self.control.warm_chat.assert_not_called()
        self.launcher.assert_not_called()

    def test_notify_warms_exact_chat_through_socket_then_uses_inbox(self) -> None:
        self.lookup.side_effect = [None, MARKER]
        self.notice.return_value = NoticeStatus.SUBMITTED
        self.assertEqual(self.notify(), claude.lifecycle.AttemptedNotice(NoticeStatus.SUBMITTED, True))
        self.control.warm_chat.assert_called_once_with(TARGET)
        self.notice.assert_called_once_with(MARKER, "message", SENDER)
        self.launcher.assert_not_called()

    def test_socket_unavailable_is_reported_without_deep_link(self) -> None:
        self.lookup.return_value = None
        self.control.warm_chat.return_value = Unavailable(reason="Desktop socket unavailable.")
        self.assertEqual(self.notify(), claude.lifecycle.StoredNotice("Desktop socket unavailable."))
        self.launcher.assert_not_called()

    def test_notify_without_worker_leaves_message_stored(self) -> None:
        self.lookup.return_value = None
        self.assertIsInstance(self.notify(), claude.lifecycle.StoredNotice)

    def test_draft_encodes_prompt_as_data(self) -> None:
        prompt = "Check A & B? folder=/elsewhere"
        with TemporaryDirectory(prefix="claudex test ") as folder:
            url = urlsplit(claude.lifecycle.draft_url(prompt, folder))
        self.assertEqual(parse_qs(url.query), {"q": [prompt], "folder": [folder]})

    def test_open_draft_uses_native_code_draft_path(self) -> None:
        claude.lifecycle.open_draft("Show the current project")
        url = urlsplit(self.launcher.call_args.args[0])
        self.assertEqual((url.scheme, url.netloc, url.path), ("claude", "code", "/new"))


class LauncherTest(unittest.TestCase):
    def test_requires_explicit_launcher(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(claude.chats.ClaudeDesktopError, "CLAUDEX_CLAUDE_LAUNCHER"):
                claude.lifecycle.launch()

    def test_launch_passes_only_the_desktop_environment(self) -> None:
        environment = {"HOME": "/desktop-home", "DISPLAY": ":0"}
        with (
            patch.object(claude.lifecycle, "_launcher", return_value=Path("/normal-launcher")),
            patch.object(claude.lifecycle, "desktop_environment", return_value=environment),
            patch("claudex.claude.lifecycle.subprocess.Popen") as start,
        ):
            claude.lifecycle.launch("claude://code/new")
        self.assertEqual(start.call_args.kwargs["env"], environment)

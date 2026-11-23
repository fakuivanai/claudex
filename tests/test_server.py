from asyncio import timeout
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import create_autospec, patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import RequestParams

from claudex.server import Bridge, build, codex_sender
from claudex.claude.lifecycle import StoredNotice
from claudex.claude.control import DesktopControl, DesktopMutation, DesktopChat, DesktopChatList, Success
from claudex.models import Provider, Sender
from claudex.mailbox import Mailbox


CODEX_CHAT = "11111111-1111-4111-8111-111111111111"
CLAUDE_CHAT = "local_22222222-2222-4222-8222-222222222222"
CODEX_SENDER = Sender(Provider.CODEX, CODEX_CHAT, "Codex review")
CLAUDE_SENDER = Sender(Provider.CLAUDE, CLAUDE_CHAT, "Claude review")


class BridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.control = create_autospec(DesktopControl, instance=True)
        self.control.get_chat.return_value = Success(result=DesktopChat(
            chat_id=CLAUDE_CHAT, title="Claude review", running=False, archived=False, warm=False,
            remote_control="off", remote_control_saved=None, model=None, effort=None,
            fast_mode=None, permission_mode=None,
        ))
        self.bridge = Bridge(Provider.CLAUDE, Mailbox(Path(temporary.name) / "mail.sqlite"), self.control)

    def test_sender_identity_comes_from_request(self) -> None:
        metadata = RequestParams.Meta.model_validate({"x-codex-turn-metadata": {"thread_id": CODEX_CHAT}})
        with patch("claudex.codex.history.chat_title", return_value="Codex review"):
            self.assertEqual(codex_sender(metadata).chat_id, CODEX_CHAT)

    def test_missing_request_identity_is_rejected(self) -> None:
        with patch.dict("os.environ", {"CODEX_THREAD_ID": CODEX_CHAT}):
            with self.assertRaisesRegex(ValueError, "did not provide"):
                codex_sender(None)

    def test_unverified_metadata_keys_do_not_supply_identity(self) -> None:
        metadata = RequestParams.Meta.model_validate({
            "threadId": CODEX_CHAT,
            "openai/threadId": "33333333-3333-4333-8333-333333333333",
        })
        with self.assertRaisesRegex(ValueError, "did not provide"):
            codex_sender(metadata)

    def test_a_second_codex_destination_is_dispatched(self) -> None:
        other = "33333333-3333-4333-8333-333333333333"
        with patch("claudex.codex.ipc.deliver") as deliver:
            self.bridge.send(CLAUDE_SENDER, other, "peer body")
        self.assertEqual(deliver.call_args.args[0], other)

    def test_codex_delivery_receives_claude_identity(self) -> None:
        with patch("claudex.codex.ipc.deliver") as deliver:
            self.bridge.send(CLAUDE_SENDER, CODEX_CHAT, "peer body")
        target, message = deliver.call_args.args
        self.assertEqual(
            (target, message.sender.chat_id, message.sender.title),
            (CODEX_CHAT, CLAUDE_CHAT, CLAUDE_SENDER.title),
        )

    def test_notification_failure_still_leaves_the_original_message(self) -> None:
        with (
            patch("claudex.claude.lifecycle.notify", side_effect=OSError("offline")),
        ):
            receipt = self.bridge.send(CODEX_SENDER, CLAUDE_CHAT, "peer body")
        self.assertEqual(self.bridge.mailbox.read(CLAUDE_CHAT, 0).messages[0].message, receipt.message)

    def test_failed_native_notice_is_reported_as_stored(self) -> None:
        with (
            patch("claudex.claude.lifecycle.notify", side_effect=OSError("offline")),
        ):
            receipt = self.bridge.send(CODEX_SENDER, CLAUDE_CHAT, "peer body")
        self.assertEqual(receipt.delivery, StoredNotice("offline"))


class StdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_results_match_the_mcp_output_schemas(self) -> None:
        control = create_autospec(DesktopControl, instance=True)
        control.list_chats.return_value = Success(result=DesktopChatList(chats=[], archived_pending=False))
        control.warm_chat.return_value = Success(result=DesktopMutation(chat=None, notice="Fixture worker"))
        with TemporaryDirectory() as directory:
            application = build(Bridge(Provider.CLAUDE, Mailbox(Path(directory) / "mail.sqlite"), control))
            async with create_connected_server_and_client_session(application) as session:
                roster = await session.call_tool("claude_list_chats")
                changed = await session.call_tool("claude_wake_chat", {"chat_id": CLAUDE_CHAT})
        self.assertFalse(roster.isError)
        self.assertEqual(roster.structuredContent, {"chats": [], "archived_pending": False})
        self.assertFalse(changed.isError)
        self.assertEqual(changed.structuredContent, {"chat": None, "notice": "Fixture worker"})

    async def test_mcp_advertises_the_public_tool_names(self) -> None:
        with TemporaryDirectory() as directory:
            parameters = StdioServerParameters(
                command=sys.executable,
                cwd=Path(__file__).parents[1],
                args=["-B", "-m", "claudex", "mcp", "--client", "codex", "--state", str(Path(directory) / "mail.sqlite")],
            )
            async with timeout(10), stdio_client(parameters) as (receive, send):
                async with ClientSession(receive, send) as session:
                    await session.initialize()
                    tools = await session.list_tools()
        self.assertEqual({tool.name for tool in tools.tools}, {
            "claude_launch", "codex_list_chats", "read_chat", "send_message", "read_messages",
            "claude_open_chat_draft", "who_am_i", "list_skills", "read_skill",
            "claude_list_chats", "claude_get_chat", "claude_set_remote_control",
            "claude_wake_chat", "claude_set_model", "claude_set_effort", "claude_set_fast_mode",
            "claude_set_permission_mode", "claude_archive", "claude_unarchive", "claude_stop",
            "claude_interrupt", "codex_chat_status",
        })
        reads = {"claude_list_chats", "claude_get_chat"}
        for tool in tools.tools:
            if tool.name.startswith("claude_"):
                self.assertIsNotNone(tool.annotations)
                assert tool.annotations is not None
                if tool.name in reads:
                    self.assertIs(tool.annotations.readOnlyHint, True)
                else:
                    self.assertIs(tool.annotations.readOnlyHint, False)
                    self.assertIs(tool.annotations.idempotentHint, False)

"""Verify the two MCP connections agree on the unattended workflow."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, create_autospec, patch

from mcp.shared.memory import create_connected_server_and_client_session

from claudex.claude.chats import NoticeStatus
from claudex.claude.control import DesktopChat, DesktopControl, Effort, Success
from claudex.claude.lifecycle import AttemptedNotice
from claudex.codex import history, ipc
from claudex.mailbox import Mailbox
from claudex.models import PeerMessage, Provider, parse_codex_chat_id
from claudex.server import Bridge, build


SOURCE_CHAT = parse_codex_chat_id("00000000-0000-4000-8000-000000000001")
STARTED_CODEX_CHAT = parse_codex_chat_id("00000000-0000-4000-8000-000000000002")
TURN_ID = "00000000-0000-4000-8000-000000000003"
CODEX_META = {"x-codex-turn-metadata": {"thread_id": SOURCE_CHAT}}
REVIEW_TASK = "Read the original Codex chat and reply with the review findings."


class WorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_apps_start_and_reply_through_their_registered_tools(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            mailbox = Mailbox(folder / "mail.sqlite")
            desktop = create_autospec(DesktopControl, instance=True)

            def start_claude(
                chat_id: str, *, folder: str, model: str, effort: Effort | None, sender_chat_id: str,
            ) -> Success[DesktopChat]:
                self.assertTrue(Path(folder).is_dir())
                self.assertEqual((model, effort, sender_chat_id), ("claude-opus-5-5", "xhigh", SOURCE_CHAT))
                messages = mailbox.read(chat_id, 0).messages
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0].message.body, REVIEW_TASK)
                return Success(result=DesktopChat(
                    chat_id=chat_id, title="Claude fixture", running=True, archived=False, warm=True,
                    remote_control="off", remote_control_saved=None, model=model, effort=effort,
                    fast_mode=None, permission_mode="default",
                ))

            desktop.start_chat.side_effect = start_claude
            codex_server = build(Bridge(Provider.CODEX, mailbox, desktop))
            with patch("claudex.codex.history.chat_title", return_value="Codex fixture"):
                async with create_connected_server_and_client_session(codex_server) as codex:
                    started = await codex.call_tool("claude_start_chat", {
                        "folder": directory, "model": "claude-opus-5-5", "effort": "xhigh", "task": REVIEW_TASK,
                    }, meta=CODEX_META)
            self.assertFalse(started.isError, started.content)
            assert started.structuredContent is not None
            self.assertEqual(started.structuredContent["state"], "submitted")
            claude_id = started.structuredContent["chat_id"]

            existing = history.ChatHistory(history.Chat(SOURCE_CHAT, "Codex fixture", None, False), (), None)
            new_codex = Mock()
            new_codex.to_wire.return_value = {
                "state": "submitted", "chat_id": STARTED_CODEX_CHAT, "turn_id": TURN_ID,
            }
            running = Mock()
            running.to_wire.return_value = {
                "chat_id": STARTED_CODEX_CHAT, "loaded": True, "reachable": True,
                "activity": "running", "turn_id": TURN_ID,
            }
            interrupted = Mock()
            interrupted.to_wire.return_value = {"state": "applied", "chat_id": STARTED_CODEX_CHAT}
            with (
                patch("claudex.claude.chats.sender", return_value=SimpleNamespace(id=claude_id, title="Claude fixture")),
                patch("claudex.codex.history.read_chat", return_value=existing) as read,
                patch("claudex.codex.ipc.deliver", return_value=ipc.Submitted(SOURCE_CHAT, TURN_ID)) as reply,
                patch("claudex.codex.ipc.start_chat", return_value=new_codex) as start,
                patch("claudex.codex.ipc.chat_status", return_value=running),
                patch("claudex.codex.ipc.interrupt", return_value=interrupted) as interrupt,
            ):
                claude_server = build(Bridge(Provider.CLAUDE, mailbox, desktop))
                async with create_connected_server_and_client_session(claude_server) as claude:
                    inbox = await claude.call_tool("read_messages")
                    transcript = await claude.call_tool("read_chat", {"provider": "codex", "target": SOURCE_CHAT})
                    response = await claude.call_tool("send_message", {"target": SOURCE_CHAT, "body": "Fixture review ready."})
                    created = await claude.call_tool("codex_start_chat", {
                        "folder": directory, "model": "gpt-6.2-sol", "effort": "low", "task": "Review a fixture.",
                    })
                    status = await claude.call_tool("codex_chat_status", {"chat_id": STARTED_CODEX_CHAT})
                    stop = await claude.call_tool("codex_interrupt", {"chat_id": STARTED_CODEX_CHAT})
            for tool_result in (inbox, transcript, response, created, status, stop):
                self.assertFalse(tool_result.isError, tool_result.content)
            assert inbox.structuredContent is not None
            received = inbox.structuredContent["messages"][0]
            self.assertEqual((received["origin"], received["body"], received["sender"]["chat_id"]),
                             ("untrusted peer", REVIEW_TASK, SOURCE_CHAT))
            read.assert_called_once()
            self.assertEqual(read.call_args.args[0], SOURCE_CHAT)
            destination, reply_message = reply.call_args.args
            self.assertEqual((destination, reply_message.sender.chat_id), (SOURCE_CHAT, claude_id))
            workspace, model, effort, start_message = start.call_args.args
            self.assertEqual((workspace, model, effort), (folder.resolve(), "gpt-6.2-sol", "low"))
            self.assertIsInstance(start_message, PeerMessage)
            self.assertEqual((start_message.sender.chat_id, start_message.body), (claude_id, "Review a fixture."))
            assert status.structuredContent is not None
            self.assertEqual((status.structuredContent["activity"], status.structuredContent["turn_id"]),
                             ("running", TURN_ID))
            interrupt.assert_called_once_with(STARTED_CODEX_CHAT)

            desktop.get_chat.return_value = Success(
                result=DesktopChat.model_validate(started.structuredContent["chat"]),
            )
            with (
                patch("claudex.codex.history.chat_title", return_value="Started Codex fixture"),
                patch("claudex.claude.lifecycle.notify", return_value=AttemptedNotice(
                    NoticeStatus.SUBMITTED, opened_saved_chat=False,
                )) as notify,
            ):
                async with create_connected_server_and_client_session(codex_server) as codex:
                    response = await codex.call_tool("send_message", {
                        "target": claude_id, "body": "Codex fixture review ready.",
                    }, meta={"x-codex-turn-metadata": {"thread_id": STARTED_CODEX_CHAT}})
            self.assertFalse(response.isError, response.content)
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[0], claude_id)

            with patch("claudex.claude.chats.sender", return_value=SimpleNamespace(
                id=claude_id, title="Claude fixture",
            )):
                async with create_connected_server_and_client_session(claude_server) as claude:
                    reply_inbox = await claude.call_tool("read_messages", {"after": received["cursor"]})
            self.assertFalse(reply_inbox.isError, reply_inbox.content)
            assert reply_inbox.structuredContent is not None
            replies = reply_inbox.structuredContent["messages"]
            self.assertEqual(len(replies), 1)
            self.assertEqual((replies[0]["origin"], replies[0]["body"], replies[0]["sender"]["chat_id"]),
                             ("untrusted peer", "Codex fixture review ready.", STARTED_CODEX_CHAT))

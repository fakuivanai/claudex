"""Check command parsing and shared control-library dispatch."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from claudex import cli
from claudex import codex
from claudex.claude.control import DesktopMutation, DesktopChat, DesktopChatList, Refused, Success
from claudex.models import parse_codex_chat_id


CLAUDE_ID = "local_00000000-0000-4000-8000-000000000201"
CODEX_ID = parse_codex_chat_id("33d0f881-b4e7-4d66-badd-8192aa1dbd8f")


def invoke(*args: str) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        status = cli.main(list(args))
    return status, output.getvalue(), errors.getvalue()


class ClaudeCommands(unittest.TestCase):
    def setUp(self) -> None:
        client = patch("claudex.cli.DesktopControl", autospec=True)
        self.control = client.start().return_value
        self.addCleanup(client.stop)
        self.chat = DesktopChat(
            chat_id=CLAUDE_ID, title="Local\x1b chat", running=False, archived=False, warm=False,
            remote_control="off", remote_control_saved=True, model="default", effort=None,
            permission_mode="default", fast_mode=False,
        )

    def test_lists_saved_state_and_deferred_archives(self) -> None:
        self.control.list_chats.return_value = Success(result=DesktopChatList(chats=[self.chat], archived_pending=True))

        status, output, errors = invoke("claude", "chats")
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("worker cold", output)
        self.assertIn("Remote Control off, saved on", output)
        self.assertIn("more archived chats to load", output)
        self.assertNotIn("\x1b", output)

        status, output, errors = invoke("claude", "chats", "--json")
        self.assertEqual((status, errors), (0, ""))
        self.assertIs(json.loads(output)["chats"][0]["remote_control_saved"], True)

    def test_reads_a_single_chat_by_exact_id(self) -> None:
        self.control.get_chat.return_value = Success(result=self.chat)
        status, output, errors = invoke("claude", "chat", CLAUDE_ID, "--json")
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(json.loads(output)["chat_id"], CLAUDE_ID)
        self.control.get_chat.assert_called_once_with(CLAUDE_ID)

    def test_every_mutation_accepts_json_and_delegates_once(self) -> None:
        cases = (
            ("remote-control", ("on",), "set_remote_control", (True,)),
            ("remote-control", ("off",), "set_remote_control", (False,)),
            ("wake", (), "warm_chat", ()),
            ("model", ("sonnet",), "set_model", ("sonnet",)),
            ("effort", ("high",), "set_effort", ("high",)),
            ("effort", ("default",), "set_effort", (None,)),
            ("fast-mode", ("off",), "set_fast_mode", (False,)),
            ("permission-mode", ("plan",), "set_permission_mode", ("plan",)),
            ("archive", (), "archive", ()),
            ("unarchive", (), "unarchive", ()),
            ("stop", (), "stop", ()),
            ("interrupt", (), "interrupt", ()),
        )
        for command, arguments, method_name, values in cases:
            with self.subTest(command=command, arguments=arguments):
                self.control.reset_mock()
                method = getattr(self.control, method_name)
                method.return_value = Success(result=DesktopMutation(chat=self.chat, notice=None))
                status, output, errors = invoke("claude", command, CLAUDE_ID, *arguments, "--json")
                self.assertEqual((status, errors), (0, ""))
                self.assertEqual(json.loads(output)["chat"]["chat_id"], CLAUDE_ID)
                method.assert_called_once_with(CLAUDE_ID, *values)

    def test_native_refusal_is_nonzero_and_machine_readable(self) -> None:
        self.control.set_permission_mode.return_value = Refused(reason="Permission mode would increase access.")
        status, output, errors = invoke("claude", "permission-mode", CLAUDE_ID, "bypassPermissions", "--json")
        self.assertEqual((status, errors), (1, ""))
        self.assertEqual(json.loads(output), {"status": "refused", "reason": "Permission mode would increase access."})

    def test_rejects_invalid_toggle_before_opening_client(self) -> None:
        with self.assertRaises(SystemExit) as failure, redirect_stderr(io.StringIO()):
            cli.main(["claude", "remote-control", CLAUDE_ID, "toggle"])
        self.assertEqual(failure.exception.code, 2)
        self.control.set_remote_control.assert_not_called()


class CodexCommands(unittest.TestCase):
    def test_status_reuses_native_owner_discovery(self) -> None:
        reachable = codex.ipc.ChatStatus(CODEX_ID, "reachable")
        with patch("claudex.codex.ipc.chat_status", return_value=reachable) as status:
            code, output, errors = invoke("codex", "status", CODEX_ID, "--json")
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(json.loads(output), {"chat_id": CODEX_ID, "loaded": True, "reachable": True})
            status.assert_called_once_with(CODEX_ID)

    def test_threads_passes_search_archives_and_pagination(self) -> None:
        chat = codex.history.Chat(CODEX_ID, "Local thread", 22000, True)
        page = codex.history.ChatPage((chat,), "next")
        with patch("claudex.codex.history.list_chats", return_value=page) as list_chats:
            status, output, errors = invoke(
                "codex", "chats", "--query", "local", "--archived", "--limit", "1", "--cursor", "prior", "--json",
            )
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(json.loads(output), page.to_wire())
        list_chats.assert_called_once_with(query="local", limit=1, cursor="prior", archived=True)

    def test_history_reuses_the_typed_thread_reader(self) -> None:
        chat = codex.history.Chat(CODEX_ID, "Local thread", 22000, True)
        history = codex.history.ChatHistory(chat, (), None)
        with patch("claudex.codex.history.read_chat", return_value=history) as read_chat:
            status, output, errors = invoke("codex", "history", CODEX_ID, "--include-archived", "--json")
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(json.loads(output), history.to_wire())
        read_chat.assert_called_once_with(
            CODEX_ID, limit=codex.history.DEFAULT_TURN_PAGE, cursor=None, include_archived=True,
        )

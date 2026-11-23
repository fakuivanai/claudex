from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from claudex.claude.control import DesktopChat
from claudex.codex.history import Chat, HistoryError
from claudex.codex.ipc import ChatStatus, CodexDesktopError
from claudex.models import CodexChatId, Provider
from claudex.restart import (
    Process, RestartError, Snapshot, claude_tray, codex_reachability, desktop_processes, native_restart,
    process, quit_desktop, remote_control_report, restart, snapshot,
)


OWNER = Process(301, 400, Path("/fixture/Codex/.ChatGPT-wrapped"))
REPLACEMENT = Process(302, 490, OWNER.executable)
LAUNCHER = Path("/fixture/bin/chatgpt")
IDLE = Snapshot({}, {})
BUSY = Snapshot({"fixture-chat": "Still running"}, {})


class RestartTests(unittest.TestCase):
    def test_busy_desktop_is_refused_before_scheduling(self) -> None:
        output = io.StringIO()
        with (
            patch("claudex.restart.selected_desktop", return_value=OWNER),
            patch("claudex.restart.launcher", return_value=LAUNCHER),
            patch("claudex.restart.snapshot", return_value=BUSY),
            patch("claudex.restart.subprocess.run") as spawn,
            redirect_stdout(output),
        ):
            self.assertEqual(restart(Provider.CODEX, as_json=True), 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "busy", "running": BUSY.running})
        spawn.assert_not_called()

    def test_pending_native_work_without_an_id_blocks_restart(self) -> None:
        output = io.StringIO()
        with (
            patch("claudex.restart.selected_desktop", return_value=OWNER),
            patch("claudex.restart.launcher", return_value=LAUNCHER),
            patch("claudex.restart.codex_roster", return_value=[]),
            patch("claudex.restart.ipc.busy_chats", return_value=((), True)),
            patch("claudex.restart.subprocess.run") as spawn,
            redirect_stdout(output),
        ):
            self.assertEqual(restart(Provider.CODEX, as_json=True), 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "busy", "running": {}, "pending_work": True})
        spawn.assert_not_called()

    def test_older_codex_build_requires_explicit_force_to_allow_unknown_activity(self) -> None:
        with (
            patch("claudex.restart.codex_roster", return_value=[]),
            patch("claudex.restart.ipc.busy_chats", return_value=None),
        ):
            with self.assertRaisesRegex(RestartError, "Install the current patch"):
                snapshot(Provider.CODEX)
            self.assertFalse(snapshot(Provider.CODEX, force=True).has_work)

    def test_unreadable_codex_roster_does_not_mask_native_work(self) -> None:
        chat = CodexChatId("00000000-0000-4000-8000-000000000013")
        with (
            patch("claudex.restart.codex_roster", side_effect=HistoryError("busy database")) as roster,
            patch("claudex.restart.ipc.busy_chats", return_value=((chat,), False)),
        ):
            state = snapshot(Provider.CODEX)
            self.assertEqual(roster.call_count, 2)
            self.assertEqual(state.running, {chat: chat})
            self.assertIsNone(state.codex_chats)
            self.assertTrue(state.has_work)
            self.assertIn("reachability_unavailable", codex_reachability(state.codex_chats))

    def test_restart_waits_for_busy_chat_and_relaunches_independently(self) -> None:
        with (
            patch("claudex.restart.selected_desktop", return_value=OWNER),
            patch("claudex.restart.desktop_environment", return_value={}),
            patch("claudex.restart.snapshot", side_effect=[BUSY, BUSY, IDLE]),
            patch("claudex.restart.time.sleep"),
            patch("claudex.restart.app_server_children", return_value=()),
            patch("claudex.restart.require_separate_worker") as check_scope,
            patch("claudex.restart.quit_desktop") as quit_app,
            patch("claudex.restart.process", return_value=None),
            patch("claudex.restart.desktop_processes", return_value=()),
            patch("claudex.restart.command_path", return_value="/fixture/bin/systemd-run"),
            patch("claudex.restart.subprocess.run") as spawn,
            patch("claudex.restart.socket_ready", return_value=REPLACEMENT),
            patch("claudex.restart.codex_reachability", return_value={"reachable": 2}),
        ):
            result = native_restart(Provider.CODEX, OWNER, LAUNCHER, wait=True, force=False)
        self.assertEqual(result["status"], "restarted")
        self.assertEqual(result["reachable"], 2)
        check_scope.assert_called_once_with(OWNER)
        quit_app.assert_called_once_with(Provider.CODEX, OWNER, {})
        self.assertIn("--user", spawn.call_args.args[0])
        self.assertEqual(spawn.call_args.args[0][-1], str(LAUNCHER))

    def test_worker_rechecks_busy_state_and_does_not_quit(self) -> None:
        with (
            patch("claudex.restart.desktop_environment", return_value={}),
            patch("claudex.restart.snapshot", return_value=BUSY),
            patch("claudex.restart.quit_desktop") as quit_app,
        ):
            result = native_restart(Provider.CLAUDE, OWNER, LAUNCHER, wait=False, force=False)
        self.assertEqual(result, {"status": "busy", "running": BUSY.running})
        quit_app.assert_not_called()

    def test_failed_exit_stops_before_launching_a_second_desktop(self) -> None:
        with (
            patch("claudex.restart.selected_desktop", return_value=OWNER),
            patch("claudex.restart.desktop_environment", return_value={}),
            patch("claudex.restart.snapshot", return_value=IDLE),
            patch("claudex.restart.require_separate_worker"),
            patch("claudex.restart.quit_desktop"),
            patch("claudex.restart.wait_for", return_value=None),
            patch("claudex.restart.subprocess.run") as spawn,
        ):
            with self.assertRaisesRegex(RestartError, "exit"):
                native_restart(Provider.CLAUDE, OWNER, LAUNCHER, wait=False, force=False)
        spawn.assert_not_called()

    def test_replacement_is_not_launched_while_old_codex_server_survives(self) -> None:
        server = Process(303, 401, Path("/fixture/Codex/codex"))
        with (
            patch("claudex.restart.selected_desktop", return_value=OWNER),
            patch("claudex.restart.desktop_environment", return_value={}),
            patch("claudex.restart.snapshot", return_value=IDLE),
            patch("claudex.restart.require_separate_worker"),
            patch("claudex.restart.app_server_children", return_value=(server,)),
            patch("claudex.restart.quit_desktop"),
            patch("claudex.restart.process", side_effect=[None, server]),
            patch("claudex.restart.QUIT_SECONDS", 0),
            patch("claudex.restart.subprocess.run") as spawn,
        ):
            with self.assertRaisesRegex(RestartError, "app server is still running"):
                native_restart(Provider.CODEX, OWNER, LAUNCHER, wait=False, force=False)
        spawn.assert_not_called()

    def test_nix_claude_browser_can_be_identified_when_proc_exe_is_inaccessible(self) -> None:
        binary = Path("/nix/store/" + "a" * 32 + "-claude-desktop-5.11492.5/lib/claude-desktop/claude-desktop")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                (301, "claude-desktop", str(binary) + "\0"),
                (302, "claude-desktop", str(binary) + "\0--type=zygote\0"),
                (303, "claude-desktop", "/fixture/claude-desktop\0"),
                (304, "unrelated", str(binary) + "\0"),
            )
            for pid, comm, command in cases:
                folder = root / str(pid)
                folder.mkdir()
                (folder / "comm").write_text(comm + "\n")
                (folder / "cmdline").write_text(command)
                (folder / "stat").write_text(f"{pid} ({comm}) S 1" + " 0" * 17 + " 400\n")
            with (
                patch("claudex.restart._PROCESS_ROOT", root),
                patch("claudex.restart.Path.readlink", side_effect=PermissionError),
                patch("claudex.restart.Path.is_file", return_value=True),
                patch("claudex.restart.os.access", return_value=True),
            ):
                self.assertEqual(desktop_processes(Provider.CLAUDE), (Process(301, 400, binary),))
                self.assertIsNone(process(303))
                self.assertIsNone(process(304))

    def test_claude_tray_quit_only_uses_menu_owned_by_verified_pid(self) -> None:
        layout = [0, [0, {}, [{"data": [8, {"label": {"data": "Quit"}}, []]}]]]
        replies = [[":1.4/Tray", ":1.5/Tray"], [999], [OWNER.pid], "/Menu", layout]
        with (
            patch("claudex.restart.process", return_value=OWNER),
            patch("claudex.restart.bus", side_effect=[*replies, None]) as bus,
        ):
            quit_desktop(Provider.CLAUDE, OWNER, {})
        self.assertEqual(bus.call_args.args, ({}, "call", ":1.5", "/Menu", "com.canonical.dbusmenu",
                                              "Event", "isvu", "8", "clicked", "s", "", "0"))
        with (
            patch("claudex.restart.process", return_value=REPLACEMENT),
            patch("claudex.restart.bus", side_effect=replies[:3]),
        ):
            with self.assertRaisesRegex(RestartError, "Quit"):
                claude_tray(OWNER, {})

    def test_codex_quit_signals_only_a_still_matching_pidfd(self) -> None:
        with (
            patch("claudex.restart.process", side_effect=[REPLACEMENT, OWNER]),
            patch("claudex.restart.os.pidfd_open", return_value=17),
            patch("claudex.restart.os.close") as close,
            patch("claudex.restart.signal.pidfd_send_signal") as send,
        ):
            with self.assertRaisesRegex(RestartError, "changed"):
                quit_desktop(Provider.CODEX, OWNER, {})
            send.assert_not_called()
            quit_desktop(Provider.CODEX, OWNER, {})
        send.assert_called_once_with(17, signal.SIGTERM)
        self.assertEqual(close.call_count, 2)

    def test_claude_recovery_reports_missing_connections(self) -> None:
        chat_id = "local_00000000-0000-4000-8000-000000000011"
        missing = "local_00000000-0000-4000-8000-000000000012"
        chat = DesktopChat.model_validate({
            "chat_id": chat_id, "title": "Returned", "running": False, "archived": False, "remote_control": "on",
            "remote_control_saved": True, "warm": True, "model": None, "effort": None, "fast_mode": None,
            "permission_mode": None,
        })
        with (
            patch("claudex.restart.claude_roster", return_value=[chat]),
            patch("claudex.restart.RESTORE_SECONDS", 0),
        ):
            report = remote_control_report(Snapshot({}, {chat_id: "Returned", missing: "Missing"}))
        self.assertEqual(report["remote_control_restored"], 1)
        self.assertEqual(report["remote_control_chats"], {chat_id: "Returned"})
        self.assertEqual(report["remote_control_missing"], {missing: "Missing"})

    def test_codex_recovery_distinguishes_known_and_unknown_chats(self) -> None:
        reachable, missing, unconfirmed = (
            Chat(CodexChatId(f"00000000-0000-4000-8000-00000000000{index}"), title, None, False)
            for index, title in enumerate(("Reachable", "Missing", "Unconfirmed"), 1)
        )
        with patch("claudex.restart.ipc.chat_status", side_effect=[
            ChatStatus(reachable.id, "loadable"), ChatStatus(missing.id, "not_loaded"), CodexDesktopError("timeout"),
        ]):
            report = codex_reachability((reachable, missing, unconfirmed))
        self.assertEqual(report, {
            "saved_chats": 3, "checked": 3, "reachable": 1, "unknown": 1,
            "reachable_chats": {reachable.id: "Reachable"}, "unreachable_chats": {missing.id: "Missing"},
        })

    def test_same_desktop_caller_gets_a_detached_receipt(self) -> None:
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as runtime,
            patch("claudex.restart.selected_desktop", return_value=OWNER),
            patch("claudex.restart.launcher", return_value=LAUNCHER),
            patch("claudex.restart.snapshot", return_value=BUSY),
            patch("claudex.restart.systemd_runtime_directory", return_value=Path(runtime)),
            patch("claudex.restart.desktop_environment", return_value={}),
            patch("claudex.restart.command_path", return_value="/fixture/bin/systemd-run"),
            patch("claudex.restart.subprocess.run") as spawn,
            patch("claudex.restart.same_desktop_context", return_value=True),
            patch("claudex.restart.wait_for") as wait_for,
            redirect_stdout(output), redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(restart(Provider.CODEX, force=True, as_json=True), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "scheduled")
            self.assertTrue(Path(payload["receipt"]).is_relative_to(runtime))
        command = spawn.call_args.args[0]
        self.assertIn("--on-active=2s", command)
        self.assertIn("claudex.restart", command)
        self.assertEqual(command[-1], "force")
        wait_for.assert_not_called()

    def test_timed_out_scheduling_leaves_room_for_a_possible_receipt(self) -> None:
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as runtime,
            patch("claudex.restart.selected_desktop", return_value=OWNER),
            patch("claudex.restart.launcher", return_value=LAUNCHER),
            patch("claudex.restart.snapshot", return_value=IDLE),
            patch("claudex.restart.systemd_runtime_directory", return_value=Path(runtime)),
            patch("claudex.restart.desktop_environment", return_value={}),
            patch("claudex.restart.command_path", return_value="/fixture/bin/systemd-run"),
            patch("claudex.restart.subprocess.run", side_effect=subprocess.TimeoutExpired("systemd-run", 8)),
            redirect_stdout(output),
        ):
            self.assertEqual(restart(Provider.CODEX, as_json=True), 1)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "unknown")
            self.assertTrue(Path(payload["receipt"]).parent.is_dir())

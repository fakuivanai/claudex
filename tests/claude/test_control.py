"""Test the typed client against an isolated Unix socket."""

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import re
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast
import unittest
from unittest.mock import patch

from claudex.claude.control import (
    EFFORTS, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, PERMISSION_MODES, DesktopControl, DesktopFailure, DesktopMutation,
    DesktopResult, InvalidRequest, InvalidResponse, Refused, Success, TimedOut, Unavailable, UnsupportedQuit,
)

_CHAT_ID = "local_01234567-89ab-cdef-0123-456789abcdef"
_OTHER_CHAT_ID = "local_11111111-2222-4444-8555-666666666666"
_ROW = {
    "chat_id": _CHAT_ID, "title": "Example", "running": False,
    "archived": False, "remote_control": "off", "remote_control_saved": True,
    "warm": False, "model": "haiku", "effort": None, "fast_mode": False,
    "permission_mode": "default",
}


def frame(value: object) -> bytes:
    return json.dumps(value).encode() + b"\n"


def exchange(server: socket.socket, response: bytes, release: Event | None) -> bytes:
    connection, _ = server.accept()
    with connection:
        connection.settimeout(3)
        with connection.makefile("rb") as stream:
            request = stream.read(MAX_REQUEST_BYTES + 1)
        if release is not None:
            release.wait(3)
        if response:
            connection.sendall(response)
        return request


class DesktopControlTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime = TemporaryDirectory()
        self.addCleanup(runtime.cleanup)
        self.runtime = Path(runtime.name) / str(os.getuid())
        self.runtime.mkdir(mode=0o700)
        directory = self.runtime / "claudex"
        directory.mkdir(mode=0o700)
        self.endpoint = directory / "desktop.sock"
        self.desktop = DesktopControl(self.runtime, timeout=3)

    @contextmanager
    def reply(self, response: bytes, release: Event | None = None) -> Iterator[Future[bytes]]:
        with socket.socket(socket.AF_UNIX) as listener, ThreadPoolExecutor(max_workers=1) as pool:
            listener.settimeout(3)
            listener.bind(str(self.endpoint))
            self.endpoint.chmod(0o600)
            listener.listen()
            try:
                yield pool.submit(exchange, listener, response, release)
            finally:
                self.endpoint.unlink()

    def test_typed_roster_and_session_use_the_window_method_names(self) -> None:
        with self.reply(frame({"status": "ok", "result": {"chats": [_ROW], "archived_pending": True}})) as request:
            result = self.desktop.list_chats()
            assert isinstance(result, Success)
            roster = result.result
            self.assertTrue(roster.archived_pending)
            self.assertEqual(len(roster.chats), 1)
            self.assertEqual(roster.chats[0].model_dump(), _ROW)
            self.assertEqual(json.loads(request.result(3)), {"tool": "getSessionList", "input": {}})
        with self.reply(frame({"status": "ok", "result": _ROW})) as request:
            chat = self.desktop.get_chat(_CHAT_ID)
            assert isinstance(chat, Success)
            self.assertEqual(chat.result.model_dump(), _ROW)
            self.assertEqual(json.loads(request.result(3)), {"tool": "getSession", "input": {"chat_id": _CHAT_ID}})

    def test_start_sends_only_the_native_start_parameters(self) -> None:
        sender = "01234567-89ab-cdef-0123-456789abcdef"
        folder = str(self.runtime)
        with self.reply(frame({"status": "ok", "result": _ROW})) as request:
            result = self.desktop.start_chat(
                _CHAT_ID, folder=folder, model="haiku", effort="low", sender_chat_id=sender,
            )
            assert isinstance(result, Success)
            self.assertEqual(result.result.chat_id, _CHAT_ID)
            self.assertEqual(json.loads(request.result(3)), {
                "tool": "startSession",
                "input": {"chat_id": _CHAT_ID, "folder": folder, "model": "haiku", "effort": "low",
                          "sender_chat_id": sender},
            })

    def test_start_refuses_invalid_folder_and_does_not_accept_another_chat(self) -> None:
        sender = "01234567-89ab-cdef-0123-456789abcdef"
        for folder in ("workspace", "/tmp/workspace\0", "/tmp/\ud800"):
            with self.subTest(folder=folder):
                self.assertIsInstance(self.desktop.start_chat(
                    _CHAT_ID, folder=folder, model="sonnet", effort=None, sender_chat_id=sender,
                ), InvalidRequest)
        with self.reply(frame({"status": "ok", "result": {**_ROW, "chat_id": _OTHER_CHAT_ID}})):
            result = self.desktop.start_chat(
                _CHAT_ID, folder=str(self.runtime), model="sonnet", effort=None, sender_chat_id=sender,
            )
            assert isinstance(result, InvalidResponse)
            self.assertIn("The chat may already have started", result.reason)

    def test_quit_checks_socket_owner_before_sending(self) -> None:
        with self.reply(frame({"status": "ok", "result": {"accepted": True}})) as request:
            result = self.desktop.quit(owner_pid=os.getpid(), force=False)
            assert isinstance(result, Success)
            self.assertEqual(json.loads(request.result(3)), {"tool": "quit", "input": {"force": False}})
        with self.reply(b"") as request:
            result = self.desktop.quit(owner_pid=os.getpid() + 1, force=True)
            self.assertIsInstance(result, Refused)
            self.assertEqual(request.result(3), b"")

    def test_quit_recognizes_only_the_old_parser_response(self) -> None:
        with self.reply(frame({"status": "error", "reason": "Invalid desktop request."})):
            self.assertIsInstance(self.desktop.quit(owner_pid=os.getpid(), force=False), UnsupportedQuit)
        with self.reply(frame({"status": "refused", "reason": "Invalid desktop request."})):
            self.assertIsInstance(self.desktop.quit(owner_pid=os.getpid(), force=False), Refused)
        with self.reply(b""):
            self.assertIsInstance(self.desktop.quit(owner_pid=os.getpid(), force=False), InvalidResponse)

    def test_each_mutation_sends_its_native_method_and_payload(self) -> None:
        cases: list[tuple[str, dict[str, object], Callable[[], DesktopResult[DesktopMutation]]]] = [
            ("toggleRemoteControl", {"enabled": True}, lambda: self.desktop.set_remote_control(_CHAT_ID, True)),
            ("toggleRemoteControl", {"enabled": False}, lambda: self.desktop.set_remote_control(_CHAT_ID, False)),
            ("warmSession", {}, lambda: self.desktop.warm_chat(_CHAT_ID)),
            ("setModel", {"model": "sonnet"}, lambda: self.desktop.set_model(_CHAT_ID, "sonnet")),
            ("setEffort", {"effort": "low"}, lambda: self.desktop.set_effort(_CHAT_ID, "low")),
            ("setEffort", {"effort": None}, lambda: self.desktop.set_effort(_CHAT_ID, None)),
            ("setFastMode", {"enabled": True}, lambda: self.desktop.set_fast_mode(_CHAT_ID, True)),
            ("setPermissionMode", {"mode": "plan"}, lambda: self.desktop.set_permission_mode(_CHAT_ID, "plan")),
            ("archive", {}, lambda: self.desktop.archive(_CHAT_ID)),
            ("unarchive", {}, lambda: self.desktop.unarchive(_CHAT_ID)),
            ("stop", {}, lambda: self.desktop.stop(_CHAT_ID)),
            ("interrupt", {}, lambda: self.desktop.interrupt(_CHAT_ID)),
        ]
        for tool, fields, call in cases:
            with self.subTest(tool=tool, **fields), self.reply(
                frame({"status": "ok", "result": {"chat": _ROW, "notice": "Example notice"}}),
            ) as request:
                result = call()
                assert isinstance(result, Success)
                self.assertIsNotNone(result.result.chat)
                self.assertEqual(result.result.notice, "Example notice")
                self.assertEqual(json.loads(request.result(3)), {
                    "tool": tool,
                    "input": {"chat_id": _CHAT_ID, **fields},
                })

    def test_distinguishes_native_refusal_and_failure(self) -> None:
        with self.reply(frame({"status": "refused", "reason": "Raising permission mode requires Desktop."})):
            result = self.desktop.set_permission_mode(_CHAT_ID, "bypassPermissions")
            self.assertEqual(result, Refused(reason="Raising permission mode requires Desktop."))
        with self.reply(frame({"status": "error", "reason": "Claude's chat roster changed."})):
            failure = self.desktop.get_chat(_CHAT_ID)
            self.assertEqual(failure, DesktopFailure(reason="Claude's chat roster changed."))

    def test_unreadable_read_reply_suggests_restart_after_updating(self) -> None:
        with self.reply(frame({"status": "ok", "result": {**_ROW, "bridge_url": "private"}})):
            self.assertEqual(self.desktop.get_chat(_CHAT_ID), InvalidResponse(
                "Claude Desktop sent a reply this Claudex build can't read. "
                "If Claudex was just updated, restart Claude Desktop."
            ))

    def test_unreadable_mutation_reply_warns_to_check_before_retrying(self) -> None:
        with self.reply(frame({"status": "ok", "result": {"chat": {"chat_id": _CHAT_ID}}})):
            self.assertEqual(self.desktop.warm_chat(_CHAT_ID), InvalidResponse(
                "Claude Desktop sent a reply this Claudex build can't read. "
                "If Claudex was just updated, restart Claude Desktop. "
                "The change may already have taken effect. Check the chat before retrying."
            ))

    def test_disallows_mismatched_chat_ids_and_response_frames(self) -> None:
        with self.reply(frame({"status": "ok", "result": {"chat": {**_ROW, "chat_id": _OTHER_CHAT_ID}}})):
            self.assertEqual(self.desktop.warm_chat(_CHAT_ID), InvalidResponse(
                "Desktop returned a different chat than requested. "
                "The change may already have taken effect. Check the chat before retrying."
            ))
        with self.reply(frame({"status": "ok", "result": _ROW}) * 2):
            self.assertEqual(
                self.desktop.get_chat(_CHAT_ID), InvalidResponse("Desktop returned an invalid response frame."),
            )

    def test_rejects_malformed_arguments_before_connecting(self) -> None:
        for invalid in (_CHAT_ID.upper(), _CHAT_ID + "\n", "local_missing"):
            self.assertIsInstance(self.desktop.get_chat(invalid), InvalidRequest)
        self.assertIsInstance(self.desktop.set_remote_control(_CHAT_ID, cast(bool, "true")), InvalidRequest)

    def test_model_id_rejects_surrogates_and_preserves_astral_characters(self) -> None:
        for surrogate in ("\ud800", "\udfff"):
            result = self.desktop.set_model(_CHAT_ID, f"model-{surrogate}")
            assert isinstance(result, InvalidRequest)
            self.assertIn("Unicode surrogate characters", result.reason)

        model = "model-\U00010000"
        with self.reply(frame({"status": "ok", "result": {"chat": _ROW}})) as request:
            self.assertIsInstance(self.desktop.set_model(_CHAT_ID, model), Success)
            self.assertEqual(json.loads(request.result(3))["input"]["model"], model)

    def test_connects_through_systemd_runtime_when_xdg_is_missing_or_relative(self) -> None:
        for environment in ({}, {"XDG_RUNTIME_DIR": "relative"}):
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, environment, clear=True),
                patch("claudex.environment._SYSTEMD_RUNTIME_ROOT", self.runtime.parent),
                self.reply(frame({"status": "ok", "result": _ROW})),
            ):
                self.assertIsInstance(DesktopControl(timeout=3).get_chat(_CHAT_ID), Success)

    def test_uses_an_absolute_xdg_runtime_in_preference_to_systemd(self) -> None:
        with (
            patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)}),
            patch("claudex.environment._SYSTEMD_RUNTIME_ROOT", self.runtime / "missing"),
            self.reply(frame({"status": "ok", "result": _ROW})),
        ):
            self.assertIsInstance(DesktopControl(timeout=3).get_chat(_CHAT_ID), Success)

    def test_rejects_missing_or_insecure_systemd_runtime_directories(self) -> None:
        self.runtime.chmod(0o755)
        actual_uid = os.getuid()
        other_uid = actual_uid + 1
        (self.runtime.parent / str(other_uid)).mkdir(mode=0o700)
        aliases = self.runtime.parent / "aliases"
        aliases.mkdir()
        (aliases / str(actual_uid)).symlink_to(self.runtime)
        for root, uid in (
            (self.runtime.parent / "missing", actual_uid),
            (self.runtime.parent, actual_uid),
            (self.runtime.parent, other_uid),
            (aliases, actual_uid),
        ):
            with (
                self.subTest(root=root, uid=uid),
                patch.dict(os.environ, {}, clear=True),
                patch("claudex.environment._SYSTEMD_RUNTIME_ROOT", root),
                patch("claudex.environment.os.getuid", return_value=uid),
            ):
                self.assertEqual(DesktopControl(timeout=3).list_chats(), Unavailable(
                    "Set an absolute XDG_RUNTIME_DIR or use a user-owned /run/user/<uid> directory with mode 0700."
                ))

    def test_reports_an_unavailable_socket_and_a_timeout_separately(self) -> None:
        self.assertIsInstance(self.desktop.list_chats(), Unavailable)
        release = Event()
        with self.reply(b"", release):
            try:
                self.assertIsInstance(DesktopControl(self.runtime, timeout=0.05).list_chats(), TimedOut)
            finally:
                release.set()

    def test_protocol_bounds_and_setting_choices_match_the_server(self) -> None:
        directory = Path(__file__).parents[2] / "claudex" / "patch"
        server = (directory / "socket.cjs").read_text()
        for name, value in (("MAX_REQUEST_BYTES", MAX_REQUEST_BYTES), ("MAX_RESPONSE_BYTES", MAX_RESPONSE_BYTES)):
            definition = re.search(rf"^const {name} = (\d+);$", server, re.MULTILINE)
            assert definition is not None
            self.assertEqual(int(definition.group(1)), value)

        efforts = re.search(r"^const EFFORTS = new Set\((\[.*\])\);$", server, re.MULTILINE)
        modes = re.search(
            r"^const PERMISSION_MODES = (\[.*\]);$", (directory / "bridge.cjs").read_text(), re.MULTILINE,
        )
        assert efforts is not None and modes is not None
        self.assertEqual(tuple(json.loads(efforts.group(1))), EFFORTS)
        self.assertEqual(tuple(json.loads(modes.group(1))), PERMISSION_MODES)

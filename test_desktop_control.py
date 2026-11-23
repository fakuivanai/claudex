"""Test the typed client against an isolated Unix socket."""

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import json
import re
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast
import unittest

from desktop_control import (
    EFFORTS, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, PERMISSION_MODES, DesktopControl, DesktopFailure, DesktopMutation,
    DesktopResult, InvalidRequest, InvalidResponse, Refused, Success, TimedOut, Unavailable,
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
        connection.sendall(response)
        return request


class DesktopControlTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime = TemporaryDirectory()
        self.addCleanup(runtime.cleanup)
        self.runtime = Path(runtime.name)
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

    def test_reports_an_unavailable_socket_and_a_timeout_separately(self) -> None:
        self.assertIsInstance(self.desktop.list_chats(), Unavailable)
        release = Event()
        with self.reply(b"", release):
            try:
                self.assertIsInstance(DesktopControl(self.runtime, timeout=0.05).list_chats(), TimedOut)
            finally:
                release.set()

    def test_protocol_bounds_and_setting_choices_match_the_server(self) -> None:
        directory = Path(__file__).with_name("patcher")
        server = (directory / "desktop_socket.cjs").read_text()
        for name, value in (("MAX_REQUEST_BYTES", MAX_REQUEST_BYTES), ("MAX_RESPONSE_BYTES", MAX_RESPONSE_BYTES)):
            definition = re.search(rf"^const {name} = (\d+);$", server, re.MULTILINE)
            assert definition is not None
            self.assertEqual(int(definition.group(1)), value)

        efforts = re.search(r"^const EFFORTS = new Set\((\[.*\])\);$", server, re.MULTILINE)
        modes = re.search(
            r"^const PERMISSION_MODES = (\[.*\]);$", (directory / "desktop_bridge.cjs").read_text(), re.MULTILINE,
        )
        assert efforts is not None and modes is not None
        self.assertEqual(tuple(json.loads(efforts.group(1))), EFFORTS)
        self.assertEqual(tuple(json.loads(modes.group(1))), PERMISSION_MODES)

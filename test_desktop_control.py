"""Test the typed client against an isolated Unix socket."""

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from pydantic import ValidationError

from desktop_control import DesktopControl, DesktopControlError, DesktopControlRefused, DesktopMutation

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


def exchange(server: socket.socket, response: bytes) -> bytes:
    connection, _ = server.accept()
    with connection:
        connection.settimeout(3)
        with connection.makefile("rb") as stream:
            request = stream.read(4097)
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
    def reply(self, response: bytes) -> Iterator[Future[bytes]]:
        with socket.socket(socket.AF_UNIX) as listener, ThreadPoolExecutor(max_workers=1) as pool:
            listener.settimeout(3)
            listener.bind(str(self.endpoint))
            self.endpoint.chmod(0o600)
            listener.listen()
            try:
                yield pool.submit(exchange, listener, response)
            finally:
                self.endpoint.unlink()

    def test_typed_roster_and_session_use_the_window_method_names(self) -> None:
        with self.reply(frame({"status": "ok", "result": {"sessions": [_ROW], "archived_pending": True}})) as request:
            roster = self.desktop.list_sessions()
            self.assertTrue(roster.archived_pending)
            self.assertEqual(len(roster.sessions), 1)
            self.assertEqual(roster.sessions[0].model_dump(), _ROW)
            self.assertEqual(json.loads(request.result(3)), {"tool": "getSessionList", "input": {}})
        with self.reply(frame({"status": "ok", "result": _ROW})) as request:
            self.assertEqual(self.desktop.get_session(_CHAT_ID).model_dump(), _ROW)
            self.assertEqual(json.loads(request.result(3)), {"tool": "getSession", "input": {"chat_id": _CHAT_ID}})

    def test_each_mutation_sends_its_native_method_and_payload(self) -> None:
        cases: list[tuple[str, dict[str, object], Callable[[], DesktopMutation]]] = [
            ("toggleRemoteControl", {"enabled": True}, lambda: self.desktop.set_remote_control(_CHAT_ID, True)),
            ("toggleRemoteControl", {"enabled": False}, lambda: self.desktop.set_remote_control(_CHAT_ID, False)),
            ("warmSession", {}, lambda: self.desktop.warm_session(_CHAT_ID)),
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
                frame({"status": "ok", "result": {"session": _ROW, "notice": "Example notice"}}),
            ) as request:
                result = call()
                self.assertIsNotNone(result.session)
                self.assertEqual(result.notice, "Example notice")
                self.assertEqual(json.loads(request.result(3)), {
                    "tool": tool,
                    "input": {"chat_id": _CHAT_ID, **fields},
                })

    def test_distinguishes_native_refusal_and_failure(self) -> None:
        with self.reply(frame({"status": "refused", "reason": "Raising permission mode requires Desktop."})):
            with self.assertRaisesRegex(DesktopControlRefused, "Raising permission mode"):
                self.desktop.set_permission_mode(_CHAT_ID, "bypassPermissions")
        with self.reply(frame({"status": "error", "reason": "Claude's session roster changed."})):
            with self.assertRaisesRegex(DesktopControlError, "session roster changed"):
                self.desktop.get_session(_CHAT_ID)

    def test_disallows_unexpected_response_fields_and_chat_id_mismatches(self) -> None:
        with self.reply(frame({"status": "ok", "result": {**_ROW, "bridge_url": "private"}})):
            with self.assertRaisesRegex(DesktopControlError, "invalid response"):
                self.desktop.get_session(_CHAT_ID)
        with self.reply(frame({"status": "ok", "result": {"session": {**_ROW, "chat_id": _OTHER_CHAT_ID}}})):
            with self.assertRaisesRegex(DesktopControlError, "different chat"):
                self.desktop.warm_session(_CHAT_ID)
        with self.reply(frame({"status": "ok", "result": _ROW}) * 2):
            with self.assertRaisesRegex(DesktopControlError, "invalid response frame"):
                self.desktop.get_session(_CHAT_ID)

    def test_rejects_malformed_arguments_before_connecting(self) -> None:
        for invalid in (_CHAT_ID.upper(), _CHAT_ID + "\n", "local_missing"):
            with self.assertRaises(ValidationError):
                self.desktop.get_session(invalid)
        with self.assertRaises(ValidationError):
            self.desktop.set_remote_control(_CHAT_ID, cast(bool, "true"))

    def test_reports_a_missing_or_nonprivate_socket(self) -> None:
        with self.assertRaisesRegex(DesktopControlError, "socket is unavailable"):
            self.desktop.list_sessions()
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.endpoint))
            self.endpoint.chmod(0o660)
            with self.assertRaisesRegex(DesktopControlError, "mode 0600"):
                self.desktop.list_sessions()
            self.endpoint.chmod(0o600)
            self.endpoint.parent.chmod(0o755)
            with self.assertRaisesRegex(DesktopControlError, "mode 0700"):
                self.desktop.list_sessions()

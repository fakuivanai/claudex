"""Typed client for Claude Desktop's private local control socket."""

from __future__ import annotations

import math
import os
import socket
import stat
import struct
import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    BaseModel, BeforeValidator, ConfigDict, Field, RootModel, SerializeAsAny, StringConstraints, ValidationError,
)

from models import parse_claude_chat_id

_MAX_REQUEST_BYTES = 4096
_MAX_RESPONSE_BYTES = 512 * 1024
_PEER_CREDENTIALS = struct.Struct("iII")
_ChatId = Annotated[str, BeforeValidator(parse_claude_chat_id)]
_Setting = Annotated[str, StringConstraints(min_length=1, max_length=128)]
type RemoteControlState = Literal["on", "connecting", "unavailable", "off"]


class DesktopControlError(ValueError):
    """The request failed or Desktop returned an invalid response."""


class DesktopControlRefused(DesktopControlError):
    """Desktop declined the requested operation."""


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DesktopSession(_WireModel):
    chat_id: _ChatId
    title: str | None
    running: bool
    archived: bool
    remote_control: RemoteControlState
    remote_control_saved: bool | None
    warm: bool
    model: str | None
    effort: str | None
    fast_mode: bool | None
    permission_mode: str | None


class DesktopSessionRoster(_WireModel):
    sessions: list[DesktopSession]
    archived_pending: bool


class DesktopMutation(_WireModel):
    session: DesktopSession | None
    notice: str | None = None


class _Tool(StrEnum):
    GET_SESSION_LIST = "getSessionList"
    GET_SESSION = "getSession"
    TOGGLE_REMOTE_CONTROL = "toggleRemoteControl"
    WARM_SESSION = "warmSession"
    SET_MODEL = "setModel"
    SET_EFFORT = "setEffort"
    SET_FAST_MODE = "setFastMode"
    SET_PERMISSION_MODE = "setPermissionMode"
    ARCHIVE = "archive"
    UNARCHIVE = "unarchive"
    STOP = "stop"
    INTERRUPT = "interrupt"


class _ChatInput(_WireModel):
    chat_id: _ChatId


class _EnabledInput(_ChatInput):
    enabled: bool


class _ModelInput(_ChatInput):
    model: _Setting


class _EffortInput(_ChatInput):
    effort: _Setting | None


class _ModeInput(_ChatInput):
    mode: _Setting


class _Request(_WireModel):
    tool: _Tool
    input: SerializeAsAny[_WireModel]


_Result = TypeVar("_Result", bound=_WireModel)


class _Success(_WireModel, Generic[_Result]):
    status: Literal["ok"]
    result: _Result


class _Refusal(_WireModel):
    status: Literal["refused"]
    reason: str


class _Failure(_WireModel):
    status: Literal["error"]
    reason: str


type _Response[Result: _WireModel] = Annotated[
    _Success[Result] | _Refusal | _Failure, Field(discriminator="status"),
]
_ROSTER_RESPONSE = RootModel[_Response[DesktopSessionRoster]]
_SESSION_RESPONSE = RootModel[_Response[DesktopSession]]
_MUTATION_RESPONSE = RootModel[_Response[DesktopMutation]]


class DesktopControl:
    def __init__(self, runtime_directory: Path | None = None, *, timeout: float = 90) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("The Desktop socket timeout must be positive and finite.")
        self._runtime_directory = runtime_directory
        self._timeout = timeout

    def list_sessions(self) -> DesktopSessionRoster:
        return self._request(_Request(tool=_Tool.GET_SESSION_LIST, input=_WireModel()), _ROSTER_RESPONSE)

    def get_session(self, chat_id: str) -> DesktopSession:
        request = _Request(tool=_Tool.GET_SESSION, input=_ChatInput(chat_id=chat_id))
        session = self._request(request, _SESSION_RESPONSE)
        if session.chat_id != chat_id:
            raise DesktopControlError("Desktop returned a different chat than requested.")
        return session

    def set_remote_control(self, chat_id: str, enabled: bool) -> DesktopMutation:
        return self._mutate(_Tool.TOGGLE_REMOTE_CONTROL, _EnabledInput(chat_id=chat_id, enabled=enabled))

    def warm_session(self, chat_id: str) -> DesktopMutation:
        return self._mutate(_Tool.WARM_SESSION, _ChatInput(chat_id=chat_id))

    def set_model(self, chat_id: str, model: str) -> DesktopMutation:
        return self._mutate(_Tool.SET_MODEL, _ModelInput(chat_id=chat_id, model=model))

    def set_effort(self, chat_id: str, effort: str | None) -> DesktopMutation:
        return self._mutate(_Tool.SET_EFFORT, _EffortInput(chat_id=chat_id, effort=effort))

    def set_fast_mode(self, chat_id: str, enabled: bool) -> DesktopMutation:
        return self._mutate(_Tool.SET_FAST_MODE, _EnabledInput(chat_id=chat_id, enabled=enabled))

    def set_permission_mode(self, chat_id: str, mode: str) -> DesktopMutation:
        return self._mutate(_Tool.SET_PERMISSION_MODE, _ModeInput(chat_id=chat_id, mode=mode))

    def archive(self, chat_id: str) -> DesktopMutation:
        return self._mutate(_Tool.ARCHIVE, _ChatInput(chat_id=chat_id))

    def unarchive(self, chat_id: str) -> DesktopMutation:
        return self._mutate(_Tool.UNARCHIVE, _ChatInput(chat_id=chat_id))

    def stop(self, chat_id: str) -> DesktopMutation:
        return self._mutate(_Tool.STOP, _ChatInput(chat_id=chat_id))

    def interrupt(self, chat_id: str) -> DesktopMutation:
        return self._mutate(_Tool.INTERRUPT, _ChatInput(chat_id=chat_id))

    def _mutate(self, tool: _Tool, input: _ChatInput) -> DesktopMutation:
        mutation = self._request(_Request(tool=tool, input=input), _MUTATION_RESPONSE)
        if mutation.session is not None and mutation.session.chat_id != input.chat_id:
            raise DesktopControlError("Desktop returned a different chat than requested.")
        return mutation

    def _socket_path(self) -> Path:
        runtime = self._runtime_directory or Path(os.environ.get("XDG_RUNTIME_DIR", ""))
        if not runtime.is_absolute():
            raise DesktopControlError("Set an absolute XDG_RUNTIME_DIR to use the Desktop socket.")
        directory = runtime / "claudex"
        endpoint = directory / "desktop.sock"
        owner = os.geteuid()
        for path in (runtime, directory):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o700:
                raise DesktopControlError("The Desktop socket directories must belong to this user and have mode 0700.")
        info = endpoint.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o600:
            raise DesktopControlError("The Desktop socket must belong to this user and have mode 0600.")
        return endpoint

    def _request(
        self, request: _Request, response_type: type[RootModel[_Response[_Result]]],
    ) -> _Result:
        wire = request.model_dump_json().encode("utf-8") + b"\n"
        if len(wire) > _MAX_REQUEST_BYTES:
            raise DesktopControlError("Desktop request exceeds the size limit.")
        try:
            endpoint = self._socket_path()
            with socket.socket(socket.AF_UNIX) as connection:
                deadline = time.monotonic() + self._timeout
                connection.settimeout(self._timeout)
                connection.connect(str(endpoint))
                credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEER_CREDENTIALS.size)
                _, uid, _ = _PEER_CREDENTIALS.unpack(credentials)
                if uid != os.geteuid():
                    raise DesktopControlError("The Desktop socket peer belongs to another user.")
                connection.sendall(wire)
                connection.shutdown(socket.SHUT_WR)
                frame = _read_frame(connection, deadline)
        except (FileNotFoundError, ConnectionRefusedError) as error:
            raise DesktopControlError("Claude Desktop's private socket is unavailable.") from error
        except TimeoutError as error:
            raise DesktopControlError("Desktop socket timed out. Check the current session state before retrying.") from error
        except OSError as error:
            raise DesktopControlError("The Desktop socket exchange failed.") from error

        try:
            reply = response_type.model_validate_json(frame).root
        except ValidationError as error:
            raise DesktopControlError("Desktop returned an invalid response.") from error
        match reply:
            case _Success(result=result):
                return result
            case _Refusal(reason=reason):
                raise DesktopControlRefused(reason)
            case _Failure(reason=reason):
                raise DesktopControlError(reason)


def _read_frame(connection: socket.socket, deadline: float) -> bytes:
    response = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        connection.settimeout(remaining)
        chunk = connection.recv(min(65536, _MAX_RESPONSE_BYTES + 1 - len(response)))
        if not chunk:
            break
        response.extend(chunk)
        if len(response) > _MAX_RESPONSE_BYTES:
            raise DesktopControlError("Desktop response exceeds the size limit.")
    if not response.endswith(b"\n") or response.count(b"\n") != 1:
        raise DesktopControlError("Desktop returned an invalid response frame.")
    return bytes(response)

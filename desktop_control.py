"""Typed client for Claude Desktop's private local control socket."""

from __future__ import annotations

import math
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Generic, Literal, TypeVar, get_args

from pydantic import (
    BaseModel, BeforeValidator, ConfigDict, Field, RootModel, StringConstraints, ValidationError,
)

from models import parse_claude_chat_id

# One small JSON request and one roster per connection. The JS tests check
# these bounds against the server's. The timeout includes queuing and a cold
# Desktop worker startup, which can take several seconds.
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 512 * 1024
DEFAULT_TIMEOUT_SECONDS = 90
_ChatId = Annotated[str, BeforeValidator(parse_claude_chat_id)]
type RemoteControlState = Literal["on", "connecting", "unavailable", "off"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
PermissionMode = Literal["plan", "default", "acceptEdits", "auto", "bypassPermissions", "dontAsk"]
EFFORTS: tuple[Effort, ...] = get_args(Effort)
PERMISSION_MODES: tuple[PermissionMode, ...] = get_args(PermissionMode)


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DesktopChat(_WireModel):
    chat_id: _ChatId
    title: str | None
    running: bool
    archived: bool
    remote_control: RemoteControlState
    remote_control_saved: bool | None
    warm: bool
    model: str | None
    # Preserve read-only settings from older or vendor-specific saved chats.
    effort: str | None
    fast_mode: bool | None
    permission_mode: str | None


class DesktopChatList(_WireModel):
    chats: list[DesktopChat]
    archived_pending: bool


class DesktopMutation(_WireModel):
    chat: DesktopChat | None
    notice: str | None = None


# Recognize an older running Desktop after the client has been updated.
# These models are used only for error reporting.
class _LegacyChatList(DesktopChatList):
    chats: list[DesktopChat] = Field(alias="sessions")


class _LegacyMutation(DesktopMutation):
    chat: DesktopChat | None = Field(alias="session")


def _model_id(value: object) -> object:
    if not isinstance(value, str):
        return value
    # Surrogate code points cannot be encoded in the UTF-8 request.
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("Enter a model ID without Unicode surrogate characters.")
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Enter a model ID without surrounding whitespace or control characters.")
    return value


class _ChatInput(_WireModel):
    chat_id: _ChatId


class _EnabledInput(_ChatInput):
    enabled: bool


class _ModelInput(_ChatInput):
    model: Annotated[str, StringConstraints(min_length=1, max_length=128), BeforeValidator(_model_id)]


class _EffortInput(_ChatInput):
    effort: Effort | None


class _ModeInput(_ChatInput):
    mode: PermissionMode


class _ListSessions(_WireModel):
    tool: Literal["getSessionList"] = "getSessionList"
    input: _WireModel


class _GetSession(_WireModel):
    tool: Literal["getSession"] = "getSession"
    input: _ChatInput


class _ToggleRemoteControl(_WireModel):
    tool: Literal["toggleRemoteControl"] = "toggleRemoteControl"
    input: _EnabledInput


class _WarmSession(_WireModel):
    tool: Literal["warmSession"] = "warmSession"
    input: _ChatInput


class _SetModel(_WireModel):
    tool: Literal["setModel"] = "setModel"
    input: _ModelInput


class _SetEffort(_WireModel):
    tool: Literal["setEffort"] = "setEffort"
    input: _EffortInput


class _SetFastMode(_WireModel):
    tool: Literal["setFastMode"] = "setFastMode"
    input: _EnabledInput


class _SetPermissionMode(_WireModel):
    tool: Literal["setPermissionMode"] = "setPermissionMode"
    input: _ModeInput


class _Archive(_WireModel):
    tool: Literal["archive"] = "archive"
    input: _ChatInput


class _Unarchive(_WireModel):
    tool: Literal["unarchive"] = "unarchive"
    input: _ChatInput


class _Stop(_WireModel):
    tool: Literal["stop"] = "stop"
    input: _ChatInput


class _Interrupt(_WireModel):
    tool: Literal["interrupt"] = "interrupt"
    input: _ChatInput


type _MutationRequest = (
    _ToggleRemoteControl | _WarmSession | _SetModel | _SetEffort | _SetFastMode |
    _SetPermissionMode | _Archive | _Unarchive | _Stop | _Interrupt
)
type _Request = _ListSessions | _GetSession | _MutationRequest

_Result = TypeVar("_Result", bound=_WireModel)


class Success(_WireModel, Generic[_Result]):
    result: _Result
    status: Literal["ok"] = "ok"


class Refused(_WireModel):
    reason: str
    status: Literal["refused"] = "refused"


class DesktopFailure(_WireModel):
    reason: str
    status: Literal["error"] = "error"


@dataclass(frozen=True)
class Unavailable:
    reason: str
    status: Literal["unavailable"] = "unavailable"


@dataclass(frozen=True)
class TimedOut:
    reason: str
    status: Literal["timed_out"] = "timed_out"


@dataclass(frozen=True)
class InvalidResponse:
    reason: str
    status: Literal["invalid_response"] = "invalid_response"


@dataclass(frozen=True)
class InvalidRequest:
    reason: str
    status: Literal["invalid_request"] = "invalid_request"


type DesktopResult[T: _WireModel] = Success[T] | Refused | DesktopFailure | Unavailable | TimedOut | InvalidResponse | InvalidRequest
type _Response[T: _WireModel] = Annotated[Success[T] | Refused | DesktopFailure, Field(discriminator="status")]
_CHAT_LIST_RESPONSE = RootModel[_Response[DesktopChatList]]
_CHAT_RESPONSE = RootModel[_Response[DesktopChat]]
_MUTATION_RESPONSE = RootModel[_Response[DesktopMutation]]
_LEGACY_CHAT_LIST_RESPONSE = RootModel[_Response[_LegacyChatList]]
_LEGACY_MUTATION_RESPONSE = RootModel[_Response[_LegacyMutation]]

_BUILD_MISMATCH = "The Claudex client and Claude Desktop may be on different builds. Restart Claude Desktop."


class DesktopControl:
    def __init__(self, runtime_directory: Path | None = None, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("The Desktop socket timeout must be positive and finite.")
        self._runtime_directory = runtime_directory
        self._timeout = timeout

    def list_chats(self) -> DesktopResult[DesktopChatList]:
        return self._request(_ListSessions(input=_WireModel()), _CHAT_LIST_RESPONSE)

    def get_chat(self, chat_id: str) -> DesktopResult[DesktopChat]:
        try:
            request = _GetSession(input=_ChatInput(chat_id=chat_id))
        except ValidationError as error:
            return _invalid_request(error)
        result = self._request(request, _CHAT_RESPONSE)
        if isinstance(result, Success) and result.result.chat_id != chat_id:
            return InvalidResponse("Desktop returned a different chat than requested.")
        return result

    def set_remote_control(self, chat_id: str, enabled: bool) -> DesktopResult[DesktopMutation]:
        return self._mutate(lambda: _ToggleRemoteControl(input=_EnabledInput(chat_id=chat_id, enabled=enabled)))

    def warm_chat(self, chat_id: str) -> DesktopResult[DesktopMutation]:
        return self._mutate(lambda: _WarmSession(input=_ChatInput(chat_id=chat_id)))

    def set_model(self, chat_id: str, model: str) -> DesktopResult[DesktopMutation]:
        return self._mutate(lambda: _SetModel(input=_ModelInput(chat_id=chat_id, model=model)))

    def set_effort(self, chat_id: str, effort: Effort | None) -> DesktopResult[DesktopMutation]:
        return self._mutate(lambda: _SetEffort(input=_EffortInput(chat_id=chat_id, effort=effort)))

    def set_fast_mode(self, chat_id: str, enabled: bool) -> DesktopResult[DesktopMutation]:
        return self._mutate(lambda: _SetFastMode(input=_EnabledInput(chat_id=chat_id, enabled=enabled)))

    def set_permission_mode(self, chat_id: str, mode: PermissionMode) -> DesktopResult[DesktopMutation]:
        return self._mutate(lambda: _SetPermissionMode(input=_ModeInput(chat_id=chat_id, mode=mode)))

    def archive(self, chat_id: str) -> DesktopResult[DesktopMutation]:
        return self._mutate(lambda: _Archive(input=_ChatInput(chat_id=chat_id)))

    def unarchive(self, chat_id: str) -> DesktopResult[DesktopMutation]:
        return self._mutate(lambda: _Unarchive(input=_ChatInput(chat_id=chat_id)))

    def stop(self, chat_id: str) -> DesktopResult[DesktopMutation]:
        return self._mutate(lambda: _Stop(input=_ChatInput(chat_id=chat_id)))

    def interrupt(self, chat_id: str) -> DesktopResult[DesktopMutation]:
        return self._mutate(lambda: _Interrupt(input=_ChatInput(chat_id=chat_id)))

    def _mutate(self, make_request: Callable[[], _MutationRequest]) -> DesktopResult[DesktopMutation]:
        try:
            request = make_request()
        except ValidationError as error:
            return _invalid_request(error)
        result = self._request(request, _MUTATION_RESPONSE)
        if isinstance(result, Success):
            chat = result.result.chat
            if chat is not None and chat.chat_id != request.input.chat_id:
                return InvalidResponse("Desktop returned a different chat than requested.")
        return result

    def _request(
        self, request: _Request, response: type[RootModel[_Response[_Result]]],
    ) -> DesktopResult[_Result]:
        wire = request.model_dump_json().encode("utf-8") + b"\n"
        if len(wire) > MAX_REQUEST_BYTES:
            return InvalidRequest("Desktop request exceeds the size limit.")

        runtime = self._runtime_directory or Path(os.environ.get("XDG_RUNTIME_DIR", ""))
        if not runtime.is_absolute():
            return Unavailable("Set an absolute XDG_RUNTIME_DIR to use the Desktop socket.")
        endpoint = runtime / "claudex" / "desktop.sock"
        deadline = time.monotonic() + self._timeout
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(self._timeout)
                connection.connect(str(endpoint))
                connection.settimeout(_remaining(deadline))
                connection.sendall(wire)
                connection.shutdown(socket.SHUT_WR)
                frame = _read_frame(connection, deadline)
        except (FileNotFoundError, ConnectionRefusedError, PermissionError):
            return Unavailable("Claude Desktop's private socket is unavailable.")
        except TimeoutError:
            return TimedOut("Desktop socket timed out. Check the current chat state before retrying.")
        except OSError:
            return DesktopFailure(reason="The Desktop socket exchange failed.")
        if isinstance(frame, InvalidResponse):
            return frame
        try:
            return response.model_validate_json(frame).root
        except ValidationError:
            return _invalid_response(request, frame)


def _invalid_response(request: _Request, frame: bytes) -> InvalidResponse:
    try:
        if isinstance(request, _ListSessions):
            legacy_list = _LEGACY_CHAT_LIST_RESPONSE.model_validate_json(frame).root
            if isinstance(legacy_list, Success):
                return InvalidResponse(_BUILD_MISMATCH)
        elif not isinstance(request, _GetSession):
            legacy_mutation = _LEGACY_MUTATION_RESPONSE.model_validate_json(frame).root
            if isinstance(legacy_mutation, Success):
                chat = legacy_mutation.result.chat
                if chat is not None and chat.chat_id != request.input.chat_id:
                    return InvalidResponse("Desktop returned a different chat than requested.")
                return InvalidResponse(
                    f"{_BUILD_MISMATCH} The change may already have taken effect. "
                    "Check the chat's state before retrying."
                )
    except ValidationError:
        pass
    return InvalidResponse("Desktop returned an invalid response.")


def _invalid_request(error: ValidationError) -> InvalidRequest:
    details = error.errors(include_input=False, include_url=False, include_context=False)
    return InvalidRequest(details[0]["msg"])


def _remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError
    return seconds


def _read_frame(connection: socket.socket, deadline: float) -> bytes | InvalidResponse:
    response = bytearray()
    while True:
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(response)))
        if not chunk:
            break
        response.extend(chunk)
        if len(response) > MAX_RESPONSE_BYTES:
            return InvalidResponse("Desktop response exceeds the size limit.")
    if not response.endswith(b"\n") or response.count(b"\n") != 1:
        return InvalidResponse("Desktop returned an invalid response frame.")
    return bytes(response)

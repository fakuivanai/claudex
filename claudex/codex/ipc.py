"""Local IPC used by Codex Desktop 26.1103.60933."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import socket
import struct
import time
from typing import Annotated, Literal, NewType
import uuid

from pydantic import AliasPath, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, TypeAdapter

from claudex.codex import history
from claudex.codex.envelope import ToolOutput, tool_output
from claudex.models import CodexChatId, PeerMessage, parse_uuid


# Reject oversized frame lengths before allocating storage for the payload.
MAX_FRAME_BYTES = 4 * 1024 * 1024
# Bound discovery and delivery together, including responses from renderer clients.
TIMEOUT_SECONDS = 12.0
# Leave room for the turn request within the default 60-second MCP call.
COLD_LOAD_TIMEOUT_SECONDS = 35.0
COLD_POLL_SECONDS = 0.25
# The router bounds a single request independently of the client deadline.
ROUTER_TIMEOUT_MILLISECONDS = 10_000
# Read large frames in chunks and keep one socket read bounded.
READ_CHUNK_BYTES = 64 * 1024
# Request and turn identifiers are opaque strings, including vendor-prefixed IDs.
WireIdentifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]
ClientId = NewType("ClientId", str)


def parse_client_id(value: object) -> ClientId:
    return ClientId(parse_uuid(value))


CheckedClientId = Annotated[ClientId, BeforeValidator(parse_client_id)]


class CodexDesktopError(ValueError):
    pass


class OwnerNotLoaded(CodexDesktopError):
    pass


class UnsupportedOwner(CodexDesktopError):
    pass


class UnconfirmedDelivery(CodexDesktopError):
    pass


@dataclass(frozen=True)
class Submitted:
    chat_id: CodexChatId
    turn_id: str
    joined_existing: Literal[True] | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "state": "submitted", "chat_id": self.chat_id, "turn_id": self.turn_id,
            "joined_existing": self.joined_existing,
        }


@dataclass(frozen=True)
class NotLoaded:
    chat_id: CodexChatId

    def to_wire(self) -> dict[str, object]:
        return {
            "state": "not_loaded", "chat_id": self.chat_id,
            "error": "Codex Desktop could not load this chat for peer delivery.",
        }


@dataclass(frozen=True)
class Refused:
    error: str

    def to_wire(self) -> dict[str, object]:
        return {"state": "refused", "error": self.error}


@dataclass(frozen=True)
class Unknown:
    error: str

    def to_wire(self) -> dict[str, object]:
        return {"state": "unknown", "error": self.error}


type Delivery = Submitted | NotLoaded | Refused | Unknown


class Method(Enum):
    # The initialization request precedes negotiation of renderer capabilities.
    INITIALIZE = ("initialize", 0)
    # Discovery version 1 reports the owner's untrusted-app-input capability.
    DISCOVER_OWNER = ("thread-owner-discovery", 1)
    # The optional pinned host-manager patch can load an owner without navigation.
    PREPARE_THREAD = ("claudex-prepare-thread", 0)
    # Start-turn version 2 accepts output separate from user input.
    START_TURN = ("thread-follower-start-turn", 2)

    def __init__(self, wire_name: str, version: int) -> None:
        self.wire_name = wire_name
        self.version = version


class RouterPayload(BaseModel):
    model_config = ConfigDict(strict=True)


class DiscoveryRequest(RouterPayload):
    request_id: WireIdentifier = Field(validation_alias="requestId")


class ResponsePayload(RouterPayload):
    request_id: WireIdentifier = Field(validation_alias="requestId")
    result_type: str = Field(validation_alias="resultType")
    method: str | None = None
    handled_by: object = Field(default=None, validation_alias="handledByClientId")
    error: object = None
    result: object = None


class Initialization(RouterPayload):
    client_id: CheckedClientId = Field(validation_alias="clientId")


class OwnerCapability(RouterPayload):
    supports_tool_output: bool = Field(default=False, validation_alias="supportsUntrustedAppInput")


type PrepareState = Literal["available", "warming", "ready", "unavailable"]


class PrepareReply(RouterPayload):
    state: PrepareState


class StartedTurn(RouterPayload):
    turn_id: WireIdentifier = Field(validation_alias=AliasPath("result", "turn", "id"))


@dataclass(frozen=True)
class RouterRejection:
    request_id: str
    no_owner: bool


type Reply = Initialization | OwnerCapability | PrepareReply | StartedTurn


@dataclass(frozen=True)
class RouterResponse:
    request_id: str
    handled_by: ClientId
    reply: Reply


type IncomingFrame = DiscoveryRequest | RouterRejection | RouterResponse
FRAME_BODY = TypeAdapter(dict[str, object])


def parse_router_frame(payload: bytes) -> IncomingFrame | None:
    try:
        fields = FRAME_BODY.validate_json(payload, strict=True)
        match fields.get("type"):
            case "client-discovery-request":
                return DiscoveryRequest.model_validate(fields)
            case "response":
                response = ResponsePayload.model_validate(fields)
            case _:
                return None

        if response.result_type != "success":
            no_owner = response.error in ("no-client-found", "client-not-found") and response.handled_by is None
            return RouterRejection(response.request_id, no_owner)

        reply: Reply
        match response.method:
            case Method.INITIALIZE.wire_name:
                reply = Initialization.model_validate(response.result)
            case Method.DISCOVER_OWNER.wire_name:
                reply = OwnerCapability.model_validate(response.result)
            case Method.PREPARE_THREAD.wire_name:
                reply = PrepareReply.model_validate(response.result)
            case Method.START_TURN.wire_name:
                reply = StartedTurn.model_validate(response.result)
            case _:
                return None
        return RouterResponse(response.request_id, parse_client_id(response.handled_by), reply)
    except ValueError as error:
        raise CodexDesktopError("Malformed Codex Desktop IPC response") from error


class DesktopClient:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.deadline = time.monotonic() + TIMEOUT_SECONDS
        self.client_id: ClientId | None = None

    def initialize(self) -> None:
        response = self.request(Method.INITIALIZE, {"clientType": "claudex"})
        if not isinstance(response.reply, Initialization) or response.handled_by != response.reply.client_id:
            raise CodexDesktopError("Codex Desktop router did not establish a client identity")
        self.client_id = response.reply.client_id

    def set_timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CodexDesktopError("Codex Desktop IPC timed out")
        self.connection.settimeout(remaining)
        return remaining

    def receive_exactly(self, count: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < count:
            self.set_timeout()
            chunk = self.connection.recv(min(count - len(chunks), READ_CHUNK_BYTES))
            if not chunk:
                raise CodexDesktopError("Codex Desktop IPC disconnected")
            chunks.extend(chunk)
        return bytes(chunks)

    def read_frame(self) -> IncomingFrame | None:
        size = int.from_bytes(self.receive_exactly(4), "little")
        if not 0 < size <= MAX_FRAME_BYTES:
            raise CodexDesktopError("Unexpected Codex Desktop IPC frame length")
        return parse_router_frame(self.receive_exactly(size))

    def write_frame(self, frame: dict[str, object]) -> None:
        payload = json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode()
        if not 0 < len(payload) <= MAX_FRAME_BYTES:
            raise CodexDesktopError("Codex Desktop IPC request exceeds the frame limit")
        self.set_timeout()
        self.connection.sendall(struct.pack("<I", len(payload)) + payload)

    def request(self, method: Method, parameters: dict[str, object], *, target: ClientId | None = None) -> RouterResponse:
        request_id = str(uuid.uuid4())
        try:
            remaining = self.set_timeout()
        except OSError as error:
            raise CodexDesktopError("Codex Desktop IPC closed before the request") from error
        frame: dict[str, object] = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": self.client_id or "initializing-client",
            "method": method.wire_name,
            "params": parameters,
            "version": method.version,
            "timeoutMs": min(ROUTER_TIMEOUT_MILLISECONDS, max(1, int(remaining * 1000))),
        }
        if target is not None:
            frame["targetClientId"] = target
        try:
            self.write_frame(frame)
            while True:
                incoming = self.read_frame()
                if isinstance(incoming, DiscoveryRequest):
                    self.write_frame({
                        "type": "client-discovery-response",
                        "requestId": incoming.request_id,
                        "response": {"canHandle": False},
                    })
                elif incoming is not None and incoming.request_id == request_id:
                    break
        except (CodexDesktopError, OSError) as error:
            failure = UnconfirmedDelivery if method is Method.START_TURN else CodexDesktopError
            raise failure("Codex Desktop IPC closed, timed out, or returned invalid data") from error
        if isinstance(incoming, RouterRejection):
            if incoming.no_owner:
                raise OwnerNotLoaded("Codex Desktop has no loaded owner for that local chat")
            failure = UnconfirmedDelivery if method is Method.START_TURN else CodexDesktopError
            raise failure("Codex Desktop did not confirm the request")
        if target is not None and incoming.handled_by != target:
            raise UnconfirmedDelivery("Codex Desktop acknowledgement did not match the request")
        return incoming

    def owner(self, chat_id: CodexChatId) -> ClientId:
        response = self.request(Method.DISCOVER_OWNER, {"hostId": "local", "conversationId": chat_id})
        reply = response.reply
        if not isinstance(reply, OwnerCapability):
            raise CodexDesktopError("Codex Desktop acknowledgement did not match owner discovery")
        if response.handled_by == self.client_id:
            raise CodexDesktopError("Codex Desktop returned the relay as chat owner")
        if not reply.supports_tool_output:
            raise UnsupportedOwner("Codex Desktop owner does not advertise untrusted tool input support")
        return response.handled_by

    def prepare_thread(self, chat_id: CodexChatId, action: Literal["check", "warm"]) -> PrepareState:
        response = self.request(
            Method.PREPARE_THREAD, {"hostId": "local", "conversationId": chat_id, "action": action},
        )
        if not isinstance(response.reply, PrepareReply) or response.handled_by == self.client_id:
            raise CodexDesktopError("Codex Desktop did not confirm thread preparation")
        if response.reply.state == "unavailable":
            raise OwnerNotLoaded("Codex Desktop could not prepare that chat.")
        return response.reply.state

    def owner_for_delivery(self, chat_id: CodexChatId) -> ClientId:
        try:
            return self.owner(chat_id)
        except OwnerNotLoaded:
            pass
        cold_deadline = time.monotonic() + COLD_LOAD_TIMEOUT_SECONDS
        self.deadline = cold_deadline
        while self.prepare_thread(chat_id, "warm") != "ready":
            if time.monotonic() + COLD_POLL_SECONDS >= cold_deadline:
                raise OwnerNotLoaded("Codex Desktop did not finish loading that chat.")
            time.sleep(COLD_POLL_SECONDS)
        self.deadline = time.monotonic() + TIMEOUT_SECONDS
        return self.owner(chat_id)

    def deliver(self, chat_id: CodexChatId, output: ToolOutput) -> Submitted:
        owner = self.owner_for_delivery(chat_id)
        try:
            prior_turn_id = history.projected_running_turn_id(chat_id)
        except history.HistoryError:
            prior_turn_id = None
        response = self.request(
            Method.START_TURN,
            {
                "conversationId": chat_id,
                "turnStart": {"request": {"threadId": chat_id, "input": [], "toolOutput": output}},
            },
            target=owner,
        )
        if not isinstance(response.reply, StartedTurn):
            raise UnconfirmedDelivery("Codex Desktop acknowledgement did not include a turn ID")
        # Desktop acknowledges the turn ID for both a new turn and a join.
        # A lagging local projection may miss a join, so report only confirmed matches.
        turn_id = response.reply.turn_id
        return Submitted(chat_id, turn_id, joined_existing=True if turn_id == prior_turn_id else None)


@contextmanager
def desktop_client(socket_path: Path | None) -> Iterator[DesktopClient]:
    path = socket_path if socket_path is not None else Path.home() / ".codex/ipc/ipc.sock"
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as error:
        raise CodexDesktopError("Could not create Codex Desktop IPC socket") from error
    with connection:
        client = DesktopClient(connection)
        try:
            client.set_timeout()
            connection.connect(str(path))
        except OSError as error:
            raise CodexDesktopError("Could not connect to Codex Desktop IPC") from error
        client.initialize()
        yield client


@dataclass(frozen=True)
class ChatStatus:
    chat_id: CodexChatId
    state: Literal["reachable", "loadable", "not_loaded", "unsupported_owner"]

    @property
    def loaded(self) -> bool:
        return self.state in ("reachable", "unsupported_owner")

    @property
    def reachable(self) -> bool:
        return self.state in ("reachable", "loadable")

    def to_wire(self) -> dict[str, object]:
        return {"chat_id": self.chat_id, "loaded": self.loaded, "reachable": self.reachable}


def chat_status(chat_id: CodexChatId, *, socket_path: Path | None = None) -> ChatStatus:
    """Check for an eligible owner or an available background load, without starting one."""
    try:
        with desktop_client(socket_path) as client:
            try:
                client.owner(chat_id)
            except OwnerNotLoaded:
                client.prepare_thread(chat_id, "check")
                return ChatStatus(chat_id, "loadable")
    except OwnerNotLoaded:
        return ChatStatus(chat_id, "not_loaded")
    except UnsupportedOwner:
        return ChatStatus(chat_id, "unsupported_owner")
    return ChatStatus(chat_id, "reachable")


def deliver(chat_id: CodexChatId, message: PeerMessage, *, socket_path: Path | None = None) -> Delivery:
    """Submit an authorized peer message after Desktop confirms a loaded owner."""
    try:
        with desktop_client(socket_path) as client:
            return client.deliver(chat_id, tool_output(message))
    except OwnerNotLoaded:
        return NotLoaded(chat_id)
    except UnconfirmedDelivery as error:
        return Unknown(str(error))
    except CodexDesktopError as error:
        return Refused(str(error))

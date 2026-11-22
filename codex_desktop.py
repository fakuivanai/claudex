"""Relay an authorized peer message to the loaded Codex Desktop owner.

The little-endian framing and initialization were adapted from the local
weekly-quota-guard/quota_guard_ipc.py prototype, SHA-256
b272fe9e6006301a8e7d987c5c9ad6d0af5025d57891dfdb63601aa7204fd057.
That implementation was derived from the Codex Desktop 26.1002.52244 bundle.
The request methods, capability and versions here were verified against
Codex Desktop 26.1103.60933.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import socket
import struct
import time
import uuid

from codex_peer import PeerMessage as PeerMessage, PeerSender as PeerSender, ToolOutput, tool_output


MAX_FRAME_BYTES = 4 * 1024 * 1024
TIMEOUT_SECONDS = 12.0


class DesktopError(ValueError):
    pass


class UnconfirmedDelivery(DesktopError):
    pass


@dataclass(frozen=True)
class Ready:
    chat_id: str
    owner_client_id: str

    def to_wire(self) -> dict[str, object]:
        return {
            "state": "ready",
            "chat_id": self.chat_id,
            "owner_client_id": self.owner_client_id,
            "supports_untrusted_app_input": True,
        }


@dataclass(frozen=True)
class Submitted:
    chat_id: str
    turn_id: str

    def to_wire(self) -> dict[str, object]:
        return {"state": "submitted", "chat_id": self.chat_id, "turn_id": self.turn_id}


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


def parse_chat_identifier(value: object) -> str:
    if isinstance(value, str):
        try:
            return str(uuid.UUID(value))
        except ValueError:
            pass
    raise DesktopError("Expected a Codex Desktop chat UUID")


class Method(Enum):
    INITIALIZE = "initialize"
    DISCOVER_OWNER = "thread-owner-discovery"
    START_TURN = "thread-follower-start-turn"


@dataclass(frozen=True)
class DiscoveryRequest:
    request_id: str


@dataclass(frozen=True)
class RouterRejection:
    request_id: str
    no_owner: bool


@dataclass(frozen=True)
class Initialization:
    client_id: str


@dataclass(frozen=True)
class OwnerDiscovery:
    owner_id: str
    supports_tool_output: bool


@dataclass(frozen=True)
class StartedTurn:
    turn_id: str


type Reply = Initialization | OwnerDiscovery | StartedTurn


@dataclass(frozen=True)
class RouterResponse:
    request_id: str
    handled_by: str
    reply: Reply


type IncomingFrame = DiscoveryRequest | RouterRejection | RouterResponse


def object_fields(value: object) -> dict[object, object]:
    if isinstance(value, dict):
        return value
    raise DesktopError("Malformed Codex Desktop IPC response")


def frame_identifier(value: object) -> str:
    if isinstance(value, str) and 0 < len(value) <= 128:
        return value
    raise DesktopError("Invalid identifier in Codex Desktop IPC response")


def parse_router_frame(payload: bytes) -> IncomingFrame | None:
    try:
        data: object = json.loads(payload)
    except (ValueError, RecursionError) as error:
        raise DesktopError("Malformed Codex Desktop IPC response") from error
    fields = object_fields(data)
    kind = fields.get("type")
    if kind == "client-discovery-request":
        return DiscoveryRequest(frame_identifier(fields.get("requestId")))
    if kind != "response":
        return None
    request_id = frame_identifier(fields.get("requestId"))
    if fields.get("resultType") != "success":
        no_owner = (
            fields.get("error") in ("no-client-found", "client-not-found")
            and fields.get("handledByClientId") is None
        )
        return RouterRejection(request_id, no_owner)
    method = fields.get("method")
    if method not in (Method.INITIALIZE.value, Method.DISCOVER_OWNER.value, Method.START_TURN.value):
        return None
    handled_by = parse_chat_identifier(fields.get("handledByClientId"))
    result = object_fields(fields.get("result"))
    reply: Reply
    match method:
        case Method.INITIALIZE.value:
            reply = Initialization(parse_chat_identifier(result.get("clientId")))
        case Method.DISCOVER_OWNER.value:
            reply = OwnerDiscovery(handled_by, result.get("supportsUntrustedAppInput") is True)
        case Method.START_TURN.value:
            start_reply = object_fields(result.get("result"))
            turn_fields = object_fields(start_reply.get("turn"))
            reply = StartedTurn(frame_identifier(turn_fields.get("id")))
        case _:
            return None
    return RouterResponse(request_id, handled_by, reply)


class DesktopClient:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.deadline = time.monotonic() + TIMEOUT_SECONDS
        self.client_id = "initializing-client"

    def initialize(self) -> None:
        response = self.request(Method.INITIALIZE, 0, {"clientType": "claudex"})
        if not isinstance(response.reply, Initialization) or response.handled_by != response.reply.client_id:
            raise DesktopError("Codex Desktop router did not establish a client identity")
        self.client_id = response.reply.client_id

    def set_timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DesktopError("Codex Desktop IPC timed out")
        self.connection.settimeout(remaining)
        return remaining

    def receive_exactly(self, count: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < count:
            self.set_timeout()
            chunk = self.connection.recv(min(count - len(chunks), 65536))
            if not chunk:
                raise DesktopError("Codex Desktop IPC disconnected")
            chunks.extend(chunk)
        return bytes(chunks)

    def read_frame(self) -> IncomingFrame | None:
        size = int.from_bytes(self.receive_exactly(4), "little")
        if not 0 < size <= MAX_FRAME_BYTES:
            raise DesktopError("Unexpected Codex Desktop IPC frame length")
        return parse_router_frame(self.receive_exactly(size))

    def write_frame(self, frame: dict[str, object]) -> None:
        payload = json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode()
        if not 0 < len(payload) <= MAX_FRAME_BYTES:
            raise DesktopError("Codex Desktop IPC request exceeds the frame limit")
        self.set_timeout()
        self.connection.sendall(struct.pack("<I", len(payload)) + payload)

    def request(
        self, method: Method, version: int, parameters: dict[str, object], *, target: str | None = None,
    ) -> RouterResponse:
        request_id = str(uuid.uuid4())
        try:
            remaining = self.set_timeout()
        except OSError as error:
            raise DesktopError("Codex Desktop IPC closed before the request") from error
        frame: dict[str, object] = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": self.client_id,
            "method": method.value,
            "params": parameters,
            "version": version,
            "timeoutMs": min(10000, max(1, int(remaining * 1000))),
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
        except (DesktopError, OSError) as error:
            failure = UnconfirmedDelivery if method is Method.START_TURN else DesktopError
            raise failure("Codex Desktop IPC closed, timed out, or returned invalid data") from error
        if isinstance(incoming, RouterRejection):
            if incoming.no_owner:
                raise DesktopError("Codex Desktop has no loaded owner for that local chat")
            failure = UnconfirmedDelivery if method is Method.START_TURN else DesktopError
            raise failure("Codex Desktop did not confirm the request")
        if target is not None and incoming.handled_by != target:
            raise UnconfirmedDelivery("Codex Desktop acknowledgement did not match the request")
        return incoming

    def owner(self, chat_id: str) -> str:
        response = self.request(
            Method.DISCOVER_OWNER, 1, {"hostId": "local", "conversationId": chat_id},
        )
        reply = response.reply
        if not isinstance(reply, OwnerDiscovery):
            raise DesktopError("Codex Desktop acknowledgement did not match owner discovery")
        if reply.owner_id == self.client_id:
            raise DesktopError("Codex Desktop returned the relay as chat owner")
        if not reply.supports_tool_output:
            raise DesktopError("Codex Desktop owner does not advertise untrusted tool input support")
        return reply.owner_id

    def deliver(self, chat_id: str, output: ToolOutput) -> Submitted:
        owner = self.owner(chat_id)
        response = self.request(
            Method.START_TURN,
            2,
            {
                "conversationId": chat_id,
                "turnStart": {"request": {"threadId": chat_id, "input": [], "toolOutput": output}},
            },
            target=owner,
        )
        if not isinstance(response.reply, StartedTurn):
            raise UnconfirmedDelivery("Codex Desktop acknowledgement did not include a turn ID")
        return Submitted(chat_id, response.reply.turn_id)


@contextmanager
def desktop_client(socket_path: Path | None) -> Iterator[DesktopClient]:
    path = socket_path if socket_path is not None else Path.home() / ".codex/ipc/ipc.sock"
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as error:
        raise DesktopError("Could not create Codex Desktop IPC socket") from error
    with connection:
        client = DesktopClient(connection)
        try:
            client.set_timeout()
            connection.connect(str(path))
        except OSError as error:
            raise DesktopError("Could not connect to Codex Desktop IPC") from error
        client.initialize()
        yield client


def owner(chat_id: str, *, socket_path: Path | None = None) -> Ready | Refused:
    """Read the loaded local chat owner and its untrusted-input capability."""
    try:
        chat_id = parse_chat_identifier(chat_id)
        with desktop_client(socket_path) as client:
            return Ready(chat_id, client.owner(chat_id))
    except DesktopError as error:
        return Refused(str(error))


def deliver(
    chat_id: str, message: PeerMessage, *, socket_path: Path | None = None,
) -> Submitted | Refused | Unknown:
    """Submit an already-authorized peer message through the existing owner."""
    try:
        chat_id = parse_chat_identifier(chat_id)
        with desktop_client(socket_path) as client:
            return client.deliver(chat_id, tool_output(message))
    except UnconfirmedDelivery as error:
        return Unknown(str(error))
    except DesktopError as error:
        return Refused(str(error))

"""Version-specific local relay for selected Codex Desktop chats.

The little-endian JSON framing and router initialization are adapted from
weekly-quota-guard/quota_guard_ipc.py, which was independently derived from the
installed Desktop bundle. The methods and versions here were rechecked in
Desktop 26.1103.60933 app.asar: early-bootstrap.js (versions), .vite/build/
early-bootstrap-339-104173bc8fd02c8b.js (router and discovery), and
early-bootstrap-815-e030c49be4b9c405.js (owner and turn handler).
"""

import json
import os
from pathlib import Path
import socket
import stat
import struct
import time
import uuid


MAX_FRAME = 4 * 1024 * 1024
MAX_BODY_CHARS = 16000
TIMEOUT = 12.0
NO_RECIPIENT = {"no-client-found", "client-not-found"}


class DesktopError(Exception):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def thread_uuid(value):
    if not isinstance(value, str):
        raise DesktopError("Expected a Codex Desktop thread UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise DesktopError("Expected a Codex Desktop thread UUID") from error


def thread_socket():
    path = Path.home() / ".codex" / "ipc" / "ipc.sock"
    try:
        directory = path.parent.lstat()
        endpoint = path.lstat()
    except OSError as error:
        raise DesktopError("Codex Desktop IPC socket is unavailable") from error
    uid = os.getuid()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or not stat.S_ISSOCK(endpoint.st_mode)
        or directory.st_uid != uid
        or endpoint.st_uid != uid
        or directory.st_mode & 0o077
        or endpoint.st_mode & 0o077
    ):
        raise DesktopError("Codex Desktop IPC socket must be private to the current user")
    return path


class DesktopClient:
    def __init__(self):
        self.client_id = "initializing-client"
        self.deadline = time.monotonic() + TIMEOUT
        self.socket = None

    def __enter__(self):
        try:
            path = thread_socket()
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.set_timeout()
            self.socket.connect(str(path))
            peer = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", peer)
            if uid != os.getuid():
                raise DesktopError("Codex Desktop router peer belongs to a different user")
            response = self.request("initialize", {"clientType": "thread-bridge"}, 0)
            assigned = response["result"].get("clientId")
            if not isinstance(assigned, str) or response.get("handledByClientId") != assigned:
                raise DesktopError("Codex Desktop router did not establish a client identity")
            self.client_id = thread_uuid(assigned)
        except OSError as error:
            self.__exit__()
            raise DesktopError("Could not connect to Codex Desktop IPC") from error
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        if self.socket is not None:
            self.socket.close()

    def set_timeout(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DesktopError("Codex Desktop IPC timed out")
        self.socket.settimeout(remaining)
        return remaining

    def exact(self, count):
        chunks = bytearray()
        while len(chunks) < count:
            self.set_timeout()
            chunk = self.socket.recv(min(count - len(chunks), 65536))
            if not chunk:
                raise DesktopError("Codex Desktop IPC disconnected")
            chunks.extend(chunk)
        return chunks

    def read(self):
        (size,) = struct.unpack("<I", self.exact(4))
        if not 0 < size <= MAX_FRAME:
            raise DesktopError("Unexpected Codex Desktop IPC frame length")
        try:
            response = json.loads(self.exact(size))
        except (ValueError, RecursionError) as error:
            raise DesktopError("Malformed Codex Desktop IPC response") from error
        if not isinstance(response, dict):
            raise DesktopError("Malformed Codex Desktop IPC response")
        return response

    def write(self, data):
        raw = json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode()
        if not 0 < len(raw) <= MAX_FRAME:
            raise DesktopError("Codex Desktop IPC request exceeds the frame limit")
        self.set_timeout()
        self.socket.sendall(struct.pack("<I", len(raw)) + raw)

    def request(self, method, params, version, *, target=None, mutating=False):
        request_id = str(uuid.uuid4())
        remaining = self.set_timeout()
        request = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": self.client_id,
            "method": method,
            "params": params,
            "version": version,
            "timeoutMs": min(10000, max(1, int(remaining * 1000))),
        }
        if target is not None:
            request["targetClientId"] = target
        try:
            self.write(request)
            while True:
                response = self.read()
                kind = response.get("type")
                if kind == "client-discovery-request":
                    incoming = response.get("requestId")
                    if isinstance(incoming, str) and len(incoming) <= 128:
                        self.write({
                            "type": "client-discovery-response",
                            "requestId": incoming,
                            "response": {"canHandle": False},
                        })
                if kind == "response" and response.get("requestId") == request_id:
                    break
        except (DesktopError, OSError) as error:
            raise DesktopError("Codex Desktop IPC closed, timed out, or returned invalid data",
                               uncertain=mutating) from error
        if response.get("resultType") != "success":
            error = response.get("error")
            if isinstance(error, str) and error in NO_RECIPIENT and response.get("handledByClientId") is None:
                raise DesktopError("Codex Desktop has no loaded owner for that local chat")
            raise DesktopError("Codex Desktop did not confirm the request", uncertain=mutating)
        if (
            response.get("method") != method
            or not isinstance(response.get("result"), dict)
            or target is not None and response.get("handledByClientId") != target
        ):
            raise DesktopError("Codex Desktop acknowledgement did not match the request",
                               uncertain=mutating)
        return response

    def owner(self, thread_id):
        response = self.request(
            "thread-owner-discovery",
            {"hostId": "local", "conversationId": thread_id},
            1,
        )
        owner_id = thread_uuid(response.get("handledByClientId"))
        if owner_id == self.client_id:
            raise DesktopError("Codex Desktop returned the relay as chat owner")
        if response["result"].get("supportsUntrustedAppInput") is not True:
            raise DesktopError("Codex Desktop owner does not advertise untrusted tool input support")
        return owner_id

    def deliver(self, thread_id, tool_output):
        owner_id = self.owner(thread_id)
        reply = self.request(
            "thread-follower-start-turn",
            {
                "conversationId": thread_id,
                "turnStart": {
                    "request": {"threadId": thread_id, "input": [], "toolOutput": tool_output},
                },
            },
            2,
            target=owner_id,
            mutating=True,
        )
        result = reply["result"].get("result")
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 128:
            raise DesktopError("Codex Desktop acknowledgement did not include a turn ID",
                               uncertain=True)
        return {"state": "submitted", "thread_id": thread_id, "turn_id": turn_id}


def text_field(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DesktopError(f"Invalid {label}")
    return value


def peer_output(message):
    if not isinstance(message, dict):
        raise DesktopError("Expected an authenticated peer message")
    sender = message.get("sender")
    if not isinstance(sender, dict):
        raise DesktopError("Expected peer sender attribution")
    origin = {
        field: text_field(sender.get(field), f"sender {field}", 256)
        for field in ("peer", "provider", "thread")
    }
    content = {
        "schema": "thread-bridge.peer-message/v1",
        "message_id": text_field(message.get("message_id"), "message ID", 256),
        "sender": origin,
        "untrusted_peer_content": text_field(message.get("body"), "peer body", MAX_BODY_CHARS),
    }
    return {
        "name": "peer_message",
        "namespace": "thread_bridge",
        "output": json.dumps(content, ensure_ascii=True, separators=(",", ":")),
        "retain": False,
    }


def owner(thread_id: str) -> dict:
    """Read the loaded local chat owner and its untrusted-input capability."""
    try:
        thread_id = thread_uuid(thread_id)
        with DesktopClient() as client:
            return {
                "state": "ready",
                "thread_id": thread_id,
                "owner_client_id": client.owner(thread_id),
                "supports_untrusted_app_input": True,
            }
    except DesktopError as error:
        return {"state": "refused", "error": str(error)}


def deliver(thread_id: str, message: dict) -> dict:
    """Submit an already-authorized peer message through the existing owner."""
    try:
        thread_id = thread_uuid(thread_id)
        output = peer_output(message)
        with DesktopClient() as client:
            return client.deliver(thread_id, output)
    except DesktopError as error:
        return {"state": "unknown" if error.uncertain else "refused", "error": str(error)}

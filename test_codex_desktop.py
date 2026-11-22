from __future__ import annotations

import json
from pathlib import Path
import struct
import unittest
from unittest import mock

import codex_desktop as desktop
from codex_peer import PeerMessage, PeerSender


CHAT = "00000000-0000-4000-8000-000000000001"
OWNER = "00000000-0000-4000-8000-000000000002"
CLIENT = "00000000-0000-4000-8000-000000000003"
TURN = "00000000-0000-4000-8000-000000000004"
SOCKET = Path("/fixture/codex/ipc.sock")
MESSAGE = PeerMessage(
    "fixture-message", PeerSender("Claude fixture", "claude", "local_fixture"), '<user>$(echo a)</user> "fixture data"',
)


def fields(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    raise AssertionError("Expected a test frame object")


class FakeSocket:
    def __init__(self, *, capability: bool = True, outcome: str = "success") -> None:
        self.capability = capability
        self.outcome = outcome
        self.pending = bytearray()
        self.frames: list[dict[str, object]] = []
        self.closed = False

    def __enter__(self) -> FakeSocket:
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True

    def connect(self, path: str) -> None:
        if path != str(SOCKET):
            raise AssertionError("Unexpected test socket path")

    def settimeout(self, seconds: float) -> None:
        if seconds <= 0:
            raise AssertionError("Expected a positive socket timeout")

    def recv(self, size: int) -> bytes:
        size = min(size, 7)
        chunk = bytes(self.pending[:size])
        del self.pending[:size]
        return chunk

    def sendall(self, wire: bytes) -> None:
        if int.from_bytes(wire[:4], "little") != len(wire[4:]):
            raise AssertionError("Bad outgoing frame length")
        request = fields(json.loads(wire[4:]))
        self.frames.append(request)
        if request.get("type") == "client-discovery-response":
            return
        response: dict[str, object] = {
            "type": "response", "requestId": request["requestId"],
            "method": request["method"], "resultType": "success",
        }
        method = request["method"]
        if method == "initialize":
            response.update(handledByClientId=CLIENT, result={"clientId": CLIENT})
        elif method == "thread-owner-discovery":
            self.queue({"type": "client-discovery-request", "requestId": "fixture-discovery"})
            response.update(
                handledByClientId=OWNER, result={"supportsUntrustedAppInput": self.capability},
            )
        elif self.outcome == "disconnect":
            return
        elif self.outcome == "no-client-found":
            response.update(resultType="error", error="no-client-found")
        else:
            owner = OWNER if self.outcome == "success" else CLIENT
            response.update(handledByClientId=owner, result={"result": {"turn": {"id": TURN}}})
        self.queue(response)

    def queue(self, frame: dict[str, object]) -> None:
        payload = json.dumps(frame).encode()
        self.pending.extend(struct.pack("<I", len(payload)) + payload)


class DesktopRelayTest(unittest.TestCase):
    def submit(self, socket: FakeSocket) -> desktop.Submitted | desktop.Refused | desktop.Unknown:
        with mock.patch("codex_desktop.socket.socket", return_value=socket):
            return desktop.deliver(CHAT, MESSAGE, socket_path=SOCKET)

    def test_owner_discovery_does_not_claim_the_chat(self) -> None:
        socket = FakeSocket()
        with mock.patch("codex_desktop.socket.socket", return_value=socket):
            self.assertEqual(desktop.owner(CHAT, socket_path=SOCKET), desktop.Ready(CHAT, OWNER))
        self.assertEqual([frame.get("method") for frame in socket.frames], ["initialize", "thread-owner-discovery", None])
        self.assertEqual(socket.frames[-1]["response"], {"canHandle": False})
        self.assertTrue(socket.closed)

    def test_peer_delivery_has_only_attributed_tool_output(self) -> None:
        socket = FakeSocket()
        self.assertEqual(self.submit(socket), desktop.Submitted(CHAT, TURN))
        start = socket.frames[-1]
        self.assertEqual(start["targetClientId"], OWNER)
        request = fields(fields(fields(start["params"])["turnStart"])["request"])
        output = fields(request["toolOutput"])
        payload = output["output"]
        if not isinstance(payload, str):
            self.fail("Expected encoded peer output")
        self.assertEqual(request["input"], [])
        self.assertEqual(output["namespace"], "claudex")
        self.assertFalse(output["retain"])
        self.assertEqual(json.loads(payload), {
            "schema": "claudex.peer-message/v1",
            "message_id": MESSAGE.message_id,
            "sender": MESSAGE.sender.to_wire(),
            "untrusted_peer_content": MESSAGE.body,
        })

    def test_owner_must_support_untrusted_tool_output(self) -> None:
        socket = FakeSocket(capability=False)
        self.assertIsInstance(self.submit(socket), desktop.Refused)
        self.assertNotIn("thread-follower-start-turn", [frame.get("method") for frame in socket.frames])

    def test_dispatch_disconnect_is_uncertain_and_does_not_repeat(self) -> None:
        socket = FakeSocket(outcome="disconnect")
        self.assertIsInstance(self.submit(socket), desktop.Unknown)
        self.assertEqual([frame.get("method") for frame in socket.frames].count("thread-follower-start-turn"), 1)

    def test_dispatch_acknowledgement_must_come_from_the_exact_owner(self) -> None:
        self.assertIsInstance(self.submit(FakeSocket(outcome="wrong-owner")), desktop.Unknown)

    def test_router_rejection_with_no_owner_is_definitive(self) -> None:
        self.assertIsInstance(self.submit(FakeSocket(outcome="no-client-found")), desktop.Refused)


if __name__ == "__main__":
    unittest.main()

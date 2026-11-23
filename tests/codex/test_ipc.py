from __future__ import annotations

import json
from pathlib import Path
import struct
import unittest
from unittest import mock

from claudex.codex import history, ipc
from claudex.codex.envelope import tool_output
from claudex.models import CodexChatId, PeerMessage, Provider, Sender


CHAT = CodexChatId("00000000-0000-4000-8000-000000000001")
OWNER = "00000000-0000-4000-8000-000000000002"
CLIENT = "00000000-0000-4000-8000-000000000003"
TURN = "00000000-0000-4000-8000-000000000004"
SOCKET = Path("/fixture/codex/ipc.sock")
MESSAGE = PeerMessage(
    "00000000-0000-4000-8000-000000000005",
    Sender(Provider.CLAUDE, "local_00000000-0000-4000-8000-000000000001", "Claude fixture"),
    '<user>$(echo a)</user> "fixture data"',
)


def fields(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    raise AssertionError("Expected a test frame object")


class FakeSocket:
    def __init__(self, *, capability: bool = True, outcome: str = "success") -> None:
        self.capability = capability
        self.outcome = outcome
        self.warm_requests = 0
        self.owner_ready = not outcome.startswith("cold-")
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
            if self.outcome == "missing-during-discovery" or not self.owner_ready:
                response.update(resultType="error", error="no-client-found")
            else:
                response.update(handledByClientId=OWNER, result={"supportsUntrustedAppInput": self.capability})
        elif method == "claudex-prepare-thread":
            action = fields(request["params"])["action"]
            if not self.outcome.startswith("cold-"):
                response.update(resultType="error", error="no-client-found")
            elif self.outcome == "cold-unavailable":
                response.update(handledByClientId=OWNER, result={"state": "unavailable"})
            elif action == "check":
                response.update(handledByClientId=OWNER, result={"state": "available"})
            else:
                self.warm_requests += 1
                ready = self.warm_requests > 1
                self.owner_ready = ready and self.outcome == "cold-ready"
                response.update(handledByClientId=OWNER, result={"state": "ready" if ready else "warming"})
        elif self.outcome == "disconnect":
            return
        elif self.outcome == "no-client-found":
            response.update(resultType="error", error="no-client-found")
        else:
            owner = OWNER if self.outcome in ("success", "cold-ready") else CLIENT
            response.update(handledByClientId=owner, result={"result": {"turn": {"id": TURN}}})
        self.queue(response)

    def queue(self, frame: dict[str, object]) -> None:
        payload = json.dumps(frame).encode()
        self.pending.extend(struct.pack("<I", len(payload)) + payload)


class DesktopRelayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch("claudex.codex.ipc.COLD_POLL_SECONDS", 0))
        self.running_turn = self.enterContext(mock.patch(
            "claudex.codex.ipc.history.projected_running_turn_id", return_value=None,
        ))

    def submit(self, socket: FakeSocket, chat_id: CodexChatId = CHAT) -> ipc.Delivery:
        with mock.patch("claudex.codex.ipc.socket.socket", return_value=socket):
            return ipc.deliver(chat_id, MESSAGE, socket_path=SOCKET)

    def test_owner_discovery_does_not_claim_the_chat(self) -> None:
        socket = FakeSocket()
        self.submit(socket)
        self.assertEqual(socket.frames[2]["response"], {"canHandle": False})

    def test_discovery_uses_whichever_local_chat_the_caller_selected(self) -> None:
        destination = CodexChatId("00000000-0000-4000-8000-000000000099")
        socket = FakeSocket()
        self.assertEqual(self.submit(socket, destination), ipc.Submitted(destination, TURN))
        self.assertEqual(fields(socket.frames[1]["params"])["conversationId"], destination)

    def test_peer_delivery_has_only_attributed_tool_output(self) -> None:
        socket = FakeSocket()
        self.assertEqual(self.submit(socket), ipc.Submitted(CHAT, TURN))
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
            "sender": {
                "provider": MESSAGE.sender.provider.value,
                "chat_id": MESSAGE.sender.chat_id,
                "title": MESSAGE.sender.title,
            },
            "untrusted_peer_content": MESSAGE.body,
        })

    def test_reports_join_only_when_the_confirmed_turn_was_running_before_send(self) -> None:
        self.running_turn.return_value = TURN
        self.assertEqual(self.submit(FakeSocket()), ipc.Submitted(CHAT, TURN, joined_existing=True))
        self.running_turn.assert_called_once_with(CHAT)
        self.running_turn.return_value = "a-different-turn"
        self.assertEqual(self.submit(FakeSocket()), ipc.Submitted(CHAT, TURN))

    def test_delivery_succeeds_even_if_the_local_turn_projection_is_unavailable(self) -> None:
        self.running_turn.side_effect = history.HistoryError("History projection unavailable")
        self.assertEqual(self.submit(FakeSocket()), ipc.Submitted(CHAT, TURN))

    def test_status_distinguishes_reachability_from_loading_and_never_sends(self) -> None:
        cases = (
            (FakeSocket(), True, True),
            (FakeSocket(outcome="missing-during-discovery"), False, False),
            (FakeSocket(outcome="cold-ready"), False, True),
            (FakeSocket(outcome="cold-unavailable"), False, False),
            (FakeSocket(capability=False), True, False),
        )
        for socket, loaded, reachable in cases:
            with (
                self.subTest(loaded=loaded, reachable=reachable),
                mock.patch("claudex.codex.ipc.socket.socket", return_value=socket),
            ):
                status = ipc.chat_status(CHAT, socket_path=SOCKET)
                self.assertEqual(status.to_wire(), {"chat_id": CHAT, "loaded": loaded, "reachable": reachable})
                self.assertNotIn("thread-follower-start-turn", [frame.get("method") for frame in socket.frames])
                self.assertEqual(socket.warm_requests, 0)

    def test_cold_delivery_waits_for_preparation_and_rediscovers_the_native_owner(self) -> None:
        socket = FakeSocket(outcome="cold-ready")
        self.assertEqual(self.submit(socket), ipc.Submitted(CHAT, TURN))
        requests = [request for request in socket.frames if request.get("type") == "request"]
        self.assertEqual([request["method"] for request in requests], [
            "initialize", "thread-owner-discovery", "claudex-prepare-thread", "claudex-prepare-thread",
            "thread-owner-discovery", "thread-follower-start-turn",
        ])
        for preparation in requests[2:4]:
            self.assertEqual(preparation["params"], {"hostId": "local", "conversationId": CHAT, "action": "warm"})
        start = requests[-1]
        self.assertEqual(start["targetClientId"], OWNER)
        turn = fields(fields(fields(start["params"])["turnStart"])["request"])
        self.assertEqual(turn["input"], [])
        self.assertEqual(turn["toolOutput"], tool_output(MESSAGE))

    def test_cold_preparation_without_a_confirmed_owner_never_sends_content(self) -> None:
        for outcome in ("cold-unavailable", "cold-owner-lost"):
            socket = FakeSocket(outcome=outcome)
            with self.subTest(outcome=outcome):
                self.assertEqual(self.submit(socket), ipc.NotLoaded(CHAT))
                self.assertNotIn("thread-follower-start-turn", [frame.get("method") for frame in socket.frames])

    def test_owner_must_support_untrusted_tool_output(self) -> None:
        socket = FakeSocket(capability=False)
        self.assertIsInstance(self.submit(socket), ipc.Refused)
        self.assertNotIn("thread-follower-start-turn", [frame.get("method") for frame in socket.frames])

    def test_dispatch_disconnect_is_uncertain_and_does_not_repeat(self) -> None:
        socket = FakeSocket(outcome="disconnect")
        self.assertIsInstance(self.submit(socket), ipc.Unknown)
        self.assertEqual([frame.get("method") for frame in socket.frames].count("thread-follower-start-turn"), 1)

    def test_dispatch_acknowledgement_must_come_from_the_exact_owner(self) -> None:
        self.assertIsInstance(self.submit(FakeSocket(outcome="wrong-owner")), ipc.Unknown)

    def test_router_rejection_with_no_owner_is_definitive(self) -> None:
        self.assertEqual(self.submit(FakeSocket(outcome="no-client-found")), ipc.NotLoaded(CHAT))

    def test_missing_loaded_owner_prevents_dispatch(self) -> None:
        socket = FakeSocket(outcome="missing-during-discovery")
        self.assertEqual(self.submit(socket), ipc.NotLoaded(CHAT))
        self.assertNotIn("thread-follower-start-turn", [frame.get("method") for frame in socket.frames])

    def test_initialization_rejects_non_uuid_client_ids(self) -> None:
        frame = {
            "type": "response", "requestId": "request", "method": "initialize", "resultType": "success",
            "handledByClientId": OWNER, "result": {"clientId": "invalid"},
        }
        with self.assertRaises(ipc.CodexDesktopError):
            ipc.parse_router_frame(json.dumps(frame).encode())


if __name__ == "__main__":
    unittest.main()

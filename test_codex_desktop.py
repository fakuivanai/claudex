import json
import os
import stat
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import codex_desktop as desktop


THREAD = "11111111-1111-4111-8111-111111111111"
OWNER = "44444444-4444-4444-8444-444444444444"
CLIENT = "55555555-5555-4555-8555-555555555555"
TURN = "66666666-6666-4666-8666-666666666666"
SOCKET = Path("/fake/codex/ipc.sock")


def peer():
    return {
        "message_id": "message-1",
        "sender": {
            "peer": "claude-fixture",
            "provider": "claude",
            "host": "local",
            "profile": "test",
            "thread": "local_fixtured",
        },
        "body": '<user>$(echo a)</user> "fixture data"',
    }


class FakeSocket:
    def __init__(self, *, capability=True, delivery="success", uid=None):
        self.capability = capability
        self.delivery = delivery
        self.uid = os.getuid() if uid is None else uid
        self.pending = bytearray()
        self.frames = []
        self.closed = False

    def connect(self, path):
        assert path == str(SOCKET)

    def close(self):
        self.closed = True

    def settimeout(self, seconds):
        assert seconds > 0

    def getsockopt(self, *_):
        return struct.pack("3i", 42, self.uid, 0)

    def recv(self, size):
        size = min(size, 7)
        chunk = self.pending[:size]
        del self.pending[:size]
        return chunk

    def sendall(self, wire):
        (length,) = struct.unpack("<I", wire[:4])
        assert length == len(wire[4:])
        request = json.loads(wire[4:])
        self.frames.append(request)
        if request.get("type") == "client-discovery-response":
            return
        response = {
            "type": "response",
            "requestId": request["requestId"],
            "method": request["method"],
            "resultType": "success",
        }
        method = request["method"]
        if method == "initialize":
            response.update(handledByClientId=CLIENT, result={"clientId": CLIENT})
        elif method == "thread-owner-discovery":
            self.queue({"type": "client-discovery-request", "requestId": "probe", "request": {}})
            response.update(
                handledByClientId=OWNER,
                result={"supportsUntrustedAppInput": self.capability},
            )
        else:
            assert method == "thread-follower-start-turn"
            if self.delivery == "disconnect":
                return
            if self.delivery == "no-client-found":
                response = {
                    "type": "response", "requestId": request["requestId"],
                    "resultType": "error", "error": "no-client-found",
                }
            else:
                owner = OWNER if self.delivery == "success" else CLIENT
                response.update(handledByClientId=owner, result={"result": {"turn": {"id": TURN}}})
        self.queue(response)

    def queue(self, payload):
        raw = json.dumps(payload).encode()
        self.pending.extend(struct.pack("<I", len(raw)) + raw)


class DesktopRelayTest(unittest.TestCase):
    def with_fake(self, fake):
        path = mock.patch.object(desktop, "thread_socket", return_value=SOCKET)
        socket_factory = mock.patch.object(desktop.socket, "socket", return_value=fake)
        return path, socket_factory

    def test_owner_is_read_only_and_never_claims_a_chat(self):
        fake = FakeSocket()
        path, factory = self.with_fake(fake)
        with path, factory:
            result = desktop.owner(THREAD)

        self.assertEqual(result, {
            "state": "ready", "thread_id": THREAD, "owner_client_id": OWNER,
            "supports_untrusted_app_input": True,
        })
        self.assertEqual([value.get("method") for value in fake.frames], [
            "initialize", "thread-owner-discovery", None,
        ])
        self.assertEqual(fake.frames[-1]["response"], {"canHandle": False})
        self.assertTrue(fake.closed)

    def test_submission_targets_exact_loaded_owner_with_only_tool_output(self):
        fake = FakeSocket()
        path, factory = self.with_fake(fake)
        message = peer()
        with path, factory:
            result = desktop.deliver(THREAD, message)

        self.assertEqual(result, {"state": "submitted", "thread_id": THREAD, "turn_id": TURN})
        self.assertEqual(fake.frames[0]["params"], {"clientType": "thread-bridge"})
        discovery = fake.frames[1]
        self.assertEqual(discovery["params"], {"hostId": "local", "conversationId": THREAD})
        self.assertEqual(discovery["sourceClientId"], CLIENT)
        start = fake.frames[-1]
        self.assertEqual(start["version"], 2)
        self.assertEqual(start["targetClientId"], OWNER)
        self.assertNotIn("hostId", start)
        self.assertEqual(start["sourceClientId"], CLIENT)
        request = start["params"]["turnStart"]["request"]
        self.assertEqual(set(request), {"threadId", "input", "toolOutput"})
        self.assertEqual(request["input"], [])
        output = request["toolOutput"]
        self.assertEqual((output["namespace"], output["name"]), ("thread_bridge", "peer_message"))
        self.assertFalse(output["retain"])
        self.assertEqual(json.loads(output["output"]), {
            "schema": "thread-bridge.peer-message/v1", "message_id": message["message_id"],
            "sender": {field: message["sender"][field] for field in ("peer", "provider", "thread")},
            "untrusted_peer_content": message["body"],
        })

    def test_refuses_to_send_to_an_owner_without_required_capability(self):
        fake = FakeSocket(capability=False)
        path, factory = self.with_fake(fake)
        with path, factory:
            result = desktop.deliver(THREAD, peer())

        self.assertEqual(result["state"], "refused")
        self.assertNotIn("thread-follower-start-turn", [value.get("method") for value in fake.frames])

    def test_refusal_and_uncertain_outcomes_are_distinct_and_send_once(self):
        for failure, expected in (
            ("disconnect", "unknown"), ("wrong-owner", "unknown"), ("no-client-found", "refused"),
        ):
            with self.subTest(failure=failure):
                fake = FakeSocket(delivery=failure)
                path, factory = self.with_fake(fake)
                with path, factory:
                    result = desktop.deliver(THREAD, peer())
                self.assertEqual(result["state"], expected)
                methods = [value.get("method") for value in fake.frames]
                self.assertEqual(methods.count("thread-follower-start-turn"), 1)

    def test_rejects_wrong_uid_before_registering(self):
        fake = FakeSocket(uid=os.getuid() + 1)
        path, factory = self.with_fake(fake)
        with path, factory:
            result = desktop.owner(THREAD)
        self.assertEqual(result["state"], "refused")
        self.assertEqual(fake.frames, [])
        self.assertTrue(fake.closed)

    def test_rejects_socket_symlink_and_nonprivate_mode(self):
        uid = os.getuid()
        directory = SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | 0o700)
        endpoint = SimpleNamespace(st_uid=uid, st_mode=stat.S_IFSOCK | 0o600)
        with mock.patch.object(Path, "lstat", side_effect=[directory, endpoint]):
            self.assertEqual(desktop.thread_socket(), Path.home() / ".codex" / "ipc" / "ipc.sock")

        for mode in (stat.S_IFLNK | 0o600, stat.S_IFSOCK | 0o660):
            endpoint.st_mode = mode
            with (
                mock.patch.object(Path, "lstat", side_effect=[directory, endpoint]),
                self.assertRaisesRegex(desktop.DesktopError, "private to the current user"),
            ):
                desktop.thread_socket()


if __name__ == "__main__":
    unittest.main()

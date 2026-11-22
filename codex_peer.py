"""Peer output embedded in Codex Desktop turns and the history projection."""

from dataclasses import dataclass
import json
from typing import TypedDict


NAMESPACE = "claudex"
LEGACY_NAMESPACE = "thread_bridge"
TOOL_NAME = "peer_message"
SCHEMA = "claudex.peer-message/v1"
LEGACY_SCHEMA = "thread-bridge.peer-message/v1"


@dataclass(frozen=True)
class PeerSender:
    peer: str
    provider: str
    chat_id: str

    def to_wire(self) -> dict[str, str]:
        return {"peer": self.peer, "provider": self.provider, "chat_id": self.chat_id}


@dataclass(frozen=True)
class PeerMessage:
    message_id: str
    sender: PeerSender
    body: str


class ToolOutput(TypedDict):
    name: str
    namespace: str
    output: str
    retain: bool


def tool_output(message: PeerMessage) -> ToolOutput:
    return {
        "name": TOOL_NAME,
        "namespace": NAMESPACE,
        "output": json.dumps({
            "schema": SCHEMA,
            "message_id": message.message_id,
            "sender": message.sender.to_wire(),
            "untrusted_peer_content": message.body,
        }, ensure_ascii=True, separators=(",", ":")),
        "retain": False,
    }


def parse_peer_output(output: object, namespace: object, name: object) -> PeerMessage | None:
    if not isinstance(output, str) or name != TOOL_NAME or namespace not in (NAMESPACE, LEGACY_NAMESPACE):
        return None
    try:
        data: object = json.loads(output)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    envelope: dict[object, object] = data
    legacy = namespace == LEGACY_NAMESPACE
    if envelope.get("schema") != (LEGACY_SCHEMA if legacy else SCHEMA):
        return None
    sender_data = envelope.get("sender")
    body = envelope.get("untrusted_peer_content")
    message_id = envelope.get("message_id")
    if not isinstance(sender_data, dict) or not isinstance(body, str) or not isinstance(message_id, str):
        return None
    sender: dict[object, object] = sender_data
    peer = sender.get("peer")
    provider = sender.get("provider")
    chat_id = sender.get("thread" if legacy else "chat_id")
    if not isinstance(peer, str) or not isinstance(provider, str) or not isinstance(chat_id, str):
        return None
    return PeerMessage(message_id[:256], PeerSender(peer[:256], provider[:256], chat_id[:256]), body)

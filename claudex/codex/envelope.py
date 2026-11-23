"""Encode the shared peer message in Codex Desktop's tool-output format."""

from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from pydantic import Field, TypeAdapter, ValidationError

from claudex.models import MessageText, PeerMessage, Sender, UUIDText


# Identify Claudex output separately from other Codex tools and read its schema.
NAMESPACE = "claudex"
TOOL_NAME = "peer_message"
SCHEMA: Literal["claudex.peer-message/v1"] = "claudex.peer-message/v1"


class ToolOutput(TypedDict):
    name: str
    namespace: str
    output: str
    retain: bool


@dataclass(frozen=True)
class Envelope:
    schema: Literal["claudex.peer-message/v1"]
    message_id: UUIDText
    sender: Sender
    body: Annotated[MessageText, Field(alias="untrusted_peer_content")]


ENVELOPE = TypeAdapter(Envelope)


def tool_output(message: PeerMessage) -> ToolOutput:
    payload = Envelope(SCHEMA, message.message_id, message.sender, message.body)
    return {
        "name": TOOL_NAME,
        "namespace": NAMESPACE,
        "output": ENVELOPE.dump_json(payload, by_alias=True).decode(),
        "retain": False,
    }


def parse_peer_output(output: str, namespace: str, name: str) -> PeerMessage | None:
    if namespace != NAMESPACE or name != TOOL_NAME:
        return None
    try:
        payload = ENVELOPE.validate_json(output)
    except ValidationError:
        return None
    return PeerMessage(payload.message_id, payload.sender, payload.body)

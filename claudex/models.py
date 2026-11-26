"""Chat identities and peer messages shared by both desktop adapters."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, NewType
from uuid import UUID

from pydantic import AfterValidator, BeforeValidator, StringConstraints, TypeAdapter


class Provider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class MessageAuthor(StrEnum):
    DEVELOPER = "developer"
    AGENT = "agent"
    SYSTEM = "system"
    UNKNOWN = "unknown"


def parse_uuid(value: object) -> str:
    if isinstance(value, str):
        try:
            return str(UUID(value))
        except ValueError:
            pass
    raise ValueError("Expected a chat or session UUID.")


UUIDText = Annotated[str, BeforeValidator(parse_uuid)]
CodexChatId = NewType("CodexChatId", str)
ClaudeChatId = NewType("ClaudeChatId", str)


def parse_codex_chat_id(value: object) -> CodexChatId:
    return CodexChatId(parse_uuid(value))


def parse_claude_chat_id(value: object) -> ClaudeChatId:
    if isinstance(value, str) and value.startswith("local_"):
        if value[6:] == parse_uuid(value[6:]):
            return ClaudeChatId(value)
    raise ValueError("Expected an exact local Claude Desktop chat ID.")


def message_text(value: str) -> str:
    if not value.strip() or "\0" in value:
        raise ValueError("Enter text without NUL characters.")
    return value


# Peer text is stored verbatim. Bound each tool payload and mailbox row.
MAX_MESSAGE_CHARACTERS = 16000
MessageText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_CHARACTERS), AfterValidator(message_text)]


@dataclass(frozen=True, slots=True)
class Sender:
    provider: Provider
    chat_id: str
    title: str

    def to_wire(self) -> dict[str, str]:
        return {"provider": self.provider.value, "chat_id": self.chat_id, "title": self.title}


@dataclass(frozen=True, slots=True)
class PeerMessage:
    message_id: UUIDText
    sender: Sender
    body: MessageText


PEER_MESSAGE = TypeAdapter(PeerMessage)

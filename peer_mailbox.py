"""Persistent local messages. Delivery adapters have no part in mailbox storage."""

from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import sqlite3
from typing import Annotated

from pydantic import Field, TypeAdapter


class Provider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


@dataclass(frozen=True, slots=True)
class Sender:
    provider: Provider
    chat_id: Annotated[str, Field(alias="thread")]
    title: Annotated[str, Field(alias="peer")]


@dataclass(frozen=True, slots=True)
class PeerMessage:
    message_id: str
    sender: Sender
    body: str


@dataclass(frozen=True, slots=True)
class StoredMessage:
    position: int
    message: PeerMessage


@dataclass(frozen=True, slots=True)
class MessagePage:
    messages: tuple[StoredMessage, ...]
    next_cursor: int


MESSAGE_FORMAT = TypeAdapter(PeerMessage)
PAGE_SIZE = 20


def stored_message(row: tuple[object, ...]) -> StoredMessage:
    position, payload = row
    if type(position) is not int or not isinstance(payload, str):
        raise ValueError("The mailbox returned an invalid message row.")
    return StoredMessage(position, MESSAGE_FORMAT.validate_json(payload, strict=True))


class Mailbox:
    def __init__(self, path: Path) -> None:
        self.path = path

    def store(self, recipient: str, message: PeerMessage) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS mail ("
                "position INTEGER PRIMARY KEY, recipient TEXT NOT NULL, message TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO mail(recipient, message) VALUES (?, ?)",
                (recipient, MESSAGE_FORMAT.dump_json(message, by_alias=True).decode("utf-8")),
            )

    def read(self, recipient: str, after: int) -> MessagePage:
        if not self.path.is_file():
            return MessagePage((), after)
        uri = self.path.absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            initialized = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='mail'"
            ).fetchone()
            if initialized is None:
                return MessagePage((), after)
            messages = tuple(
                stored_message(row) for row in connection.execute(
                    "SELECT position, message FROM mail WHERE recipient=? AND position>? "
                    "ORDER BY position LIMIT ?",
                    (recipient, after, PAGE_SIZE),
                )
            )
        return MessagePage(messages, messages[-1].position if messages else after)

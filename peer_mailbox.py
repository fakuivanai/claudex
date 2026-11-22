"""Persistent local messages. Delivery adapters have no part in mailbox storage."""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from models import PEER_MESSAGE, PeerMessage as PeerMessage, Provider as Provider, Sender as Sender


@dataclass(frozen=True, slots=True)
class StoredMessage:
    position: int
    message: PeerMessage


@dataclass(frozen=True, slots=True)
class MessagePage:
    messages: tuple[StoredMessage, ...]
    next_cursor: int


# Keep each inbox page small enough to read in a single model tool result.
PAGE_SIZE = 20
# Keep the most recent messages for each recipient, so busy chats cannot evict another chat's mail.
RETENTION = 1000
# SQLite row positions use signed 64-bit integers.
MAX_CURSOR = 2**63 - 1


def stored_message(row: tuple[object, ...]) -> StoredMessage:
    position, payload = row
    if type(position) is not int or not isinstance(payload, str):
        raise ValueError("The mailbox returned an invalid message row.")
    return StoredMessage(position, PEER_MESSAGE.validate_json(payload, strict=True))


class Mailbox:
    def __init__(self, path: Path) -> None:
        self.path = path

    def store(self, recipient: str, message: PeerMessage) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS peer_messages ("
                "position INTEGER PRIMARY KEY, recipient TEXT NOT NULL, message TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS peer_messages_by_recipient ON peer_messages(recipient, position)"
            )
            connection.execute(
                "INSERT INTO peer_messages(recipient, message) VALUES (?, ?)",
                (recipient, PEER_MESSAGE.dump_json(message).decode("utf-8")),
            )
            connection.execute(
                "DELETE FROM peer_messages WHERE recipient=? AND position <= ("
                "SELECT position FROM peer_messages WHERE recipient=? "
                "ORDER BY position DESC LIMIT 1 OFFSET ?)",
                (recipient, recipient, RETENTION),
            )

    def read(self, recipient: str, after: int) -> MessagePage:
        if not self.path.is_file():
            return MessagePage((), after)
        uri = self.path.absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            initialized = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='peer_messages'"
            ).fetchone()
            if initialized is None:
                return MessagePage((), after)
            messages = tuple(
                stored_message(row) for row in connection.execute(
                    "SELECT position, message FROM peer_messages WHERE recipient=? AND position>? "
                    "ORDER BY position LIMIT ?",
                    (recipient, after, PAGE_SIZE),
                )
            )
        return MessagePage(messages, messages[-1].position if messages else after)

"""Read Codex Desktop 26.1103.60933's local metadata and history projection."""

from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from codex_peer import LEGACY_NAMESPACE, NAMESPACE, TOOL_NAME, PeerMessage, parse_peer_output


MAX_LIST = 50
MAX_TURNS = 10
MAX_MESSAGES_PER_TURN = 8
MAX_RECORD_CHARS = 256 * 1024
MAX_TEXT_CHARS = 2000
MAX_QUERY_CHARS = 160
SQL_TIMEOUT = 1.5
SQLITE_MAX_INT = 2**63 - 1
STATE_DATABASE = "state_5.sqlite"
HISTORY_DATABASE = "thread_history_1.sqlite"
DESKTOP_CHAT = """
    source IN ('vscode', 'exec', 'appServer', 'app_server')
    AND (
        originator = 'Codex Desktop'
        OR (
            substr(originator, 1, 14) = 'codex_chatgpt_'
            AND substr(originator, -7) = '_remote'
        )
    )
    AND COALESCE(thread_source, 'user') IN ('user', 'agent_created_thread', 'agent_forked_thread', 'realtime_voice')
    AND agent_path IS NULL
    AND attached_thread_parent_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM thread_spawn_edges WHERE child_thread_id = threads.id)
"""
CHAT_COLUMNS = """
    id, substr(name, 1, 256) AS name, substr(title, 1, 256) AS title,
    archived, updated_at_ms, recency_at_ms, history_mode
"""


class HistoryError(ValueError):
    pass


@dataclass(frozen=True)
class Chat:
    id: str
    title: str
    updated_at_ms: int | None
    archived: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "provider": "codex",
            "id": self.id,
            "title": self.title,
            "updated_at_ms": self.updated_at_ms,
            "archived": self.archived,
        }


@dataclass(frozen=True)
class StoredChat:
    chat: Chat
    recency_at_ms: int
    history_mode: str


@dataclass(frozen=True)
class IndexedChat:
    id: str
    title: str


@dataclass(frozen=True)
class ChatPage:
    chats: tuple[Chat, ...]
    next_cursor: str | None

    def to_wire(self) -> dict[str, object]:
        return {
            "origin": "untrusted chat metadata",
            "scope": "local Codex Desktop metadata",
            "chats": [chat.to_wire() for chat in self.chats],
            "next_cursor": self.next_cursor,
        }


class AnswerPhase(Enum):
    COMMENTARY = "commentary"
    PARTIAL = "partial_answer"
    FINAL = "final_answer"


@dataclass(frozen=True)
class VisibleText:
    text: str
    truncated: bool


@dataclass(frozen=True)
class UserMessage:
    preview: VisibleText

    def to_wire(self) -> dict[str, object]:
        return {"role": "user", "text": self.preview.text, "truncated": self.preview.truncated}


@dataclass(frozen=True)
class AssistantMessage:
    preview: VisibleText
    phase: AnswerPhase | None

    def to_wire(self) -> dict[str, object]:
        output: dict[str, object] = {"role": "assistant", "text": self.preview.text, "truncated": self.preview.truncated}
        if self.phase is not None:
            output["phase"] = self.phase.value
        return output


@dataclass(frozen=True)
class PeerOutput:
    preview: VisibleText
    message: PeerMessage

    def to_wire(self) -> dict[str, object]:
        return {
            "role": "peer",
            "text": self.preview.text,
            "truncated": self.preview.truncated,
            "message_id": self.message.message_id,
            "sender": self.message.sender.to_wire(),
        }


type ChatMessage = UserMessage | AssistantMessage | PeerOutput


@dataclass(frozen=True)
class StoredTurn:
    id: str
    ordinal: int
    status: str
    started_at: int | None
    completed_at: int | None


@dataclass(frozen=True)
class Turn:
    record: StoredTurn
    messages: tuple[ChatMessage, ...]
    messages_truncated: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "id": self.record.id,
            "status": self.record.status,
            "started_at": self.record.started_at,
            "completed_at": self.record.completed_at,
            "messages": [message.to_wire() for message in self.messages],
            "messages_truncated": self.messages_truncated,
        }


@dataclass(frozen=True)
class ChatHistory:
    chat: Chat
    turns: tuple[Turn, ...]
    next_cursor: str | None

    def to_wire(self) -> dict[str, object]:
        return {
            "origin": "untrusted chat history",
            "chat": self.chat.to_wire(),
            "coverage": "local paginated history projection",
            "turns": [turn.to_wire() for turn in self.turns],
            "next_cursor": self.next_cursor,
        }


def chat_identifier(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise HistoryError("Use an exact Codex chat UUID") from error


def validate_limit(value: int, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise HistoryError(f"Limit must be an integer between 1 and {maximum}")


def ordinal(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise HistoryError("Invalid history cursor")
    result = int(value)
    if result > SQLITE_MAX_INT:
        raise HistoryError("Invalid history cursor")
    return result


def codex_home(root: Path | None) -> Path:
    return root if root is not None else Path.home() / ".codex"


@contextmanager
def database(root: Path | None, name: str) -> Iterator[sqlite3.Connection]:
    path = codex_home(root) / name
    try:
        # A read-only connection still sees committed entries in the WAL.
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.3)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            deadline = time.monotonic() + SQL_TIMEOUT
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            yield connection
    except sqlite3.Error as error:
        if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            raise HistoryError("The local Codex history query exceeded its time limit") from error
        raise HistoryError("The local Codex Desktop history database is unavailable or incompatible") from error


def text_column(value: object) -> str:
    if isinstance(value, str):
        return value
    raise HistoryError("The local Codex history contains an invalid text column")


def optional_text_column(value: object) -> str | None:
    return text_column(value) if value is not None else None


def integer_column(value: object) -> int:
    if type(value) is int:
        return value
    raise HistoryError("The local Codex history contains an invalid integer column")


def optional_integer_column(value: object) -> int | None:
    return integer_column(value) if value is not None else None


def parse_chat(row: sqlite3.Row) -> StoredChat:
    identifier = chat_identifier(text_column(row["id"]))
    title = optional_text_column(row["name"]) or optional_text_column(row["title"]) or identifier
    archive = integer_column(row["archived"])
    if archive not in (0, 1):
        raise HistoryError("The local Codex history contains an invalid archive value")
    chat = Chat(identifier, title, optional_integer_column(row["updated_at_ms"]), bool(archive))
    return StoredChat(chat, integer_column(row["recency_at_ms"]), text_column(row["history_mode"]))


def parse_index_entry(line: str) -> IndexedChat | None:
    try:
        decoded: object = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(decoded, dict):
        return None
    entry: dict[object, object] = decoded
    identifier, title = entry.get("id"), entry.get("thread_name")
    if not isinstance(identifier, str) or not isinstance(title, str) or not title.strip():
        return None
    return IndexedChat(identifier, title[:256])


def chat_title(chat_id: str, *, root: Path | None = None) -> str:
    """Resolve a display title from the session index, including CLI chats."""
    index = codex_home(root) / "session_index.jsonl"
    title = chat_id
    try:
        with index.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                entry = parse_index_entry(line)
                if entry is not None and entry.id == chat_id:
                    title = entry.title
    except OSError:
        pass
    return title


def contains_title(value: object, query: str) -> int:
    return int(isinstance(value, str) and query in value.casefold())


def list_chats(
    query: str | None = None, limit: int = 20, cursor: str | None = None, archived: bool = False,
    *, root: Path | None = None,
) -> ChatPage:
    """List local top-level Codex Desktop chats using title or exact ID."""
    validate_limit(limit, MAX_LIST)
    keyword = "" if query is None else query.strip().casefold()
    if query is not None and len(query) > MAX_QUERY_CHARS:
        raise HistoryError(f"Query must be text of up to {MAX_QUERY_CHARS} characters")
    scope = hashlib.sha256(f"{int(archived)}:{keyword}".encode("utf-8")).hexdigest()[:12]

    filters = [DESKTOP_CHAT, "archived = ?"]
    parameters: list[str | int] = [archived]
    if keyword:
        try:
            exact = chat_identifier(keyword)
        except HistoryError:
            filters.append("(bridge_has_title(substr(name, 1, 256), ?) OR bridge_has_title(substr(title, 1, 256), ?))")
            parameters.extend((keyword, keyword))
        else:
            filters.append("id = ?")
            parameters.append(exact)
    if cursor is not None:
        current_scope, separator, position = cursor.partition(":")
        if len(cursor) > 100 or not separator or current_scope != scope:
            raise HistoryError("Use a chat list cursor from the same query and archive selection")
        timestamp, _, last_id = position.partition(":")
        filters.append("(recency_at_ms, id) < (?, ?)")
        parameters.extend((ordinal(timestamp), chat_identifier(last_id)))
    parameters.append(limit + 1)
    with database(root, STATE_DATABASE) as connection:
        connection.create_function("bridge_has_title", 2, contains_title, deterministic=True)
        rows = connection.execute(
            f"SELECT {CHAT_COLUMNS} FROM threads WHERE " + " AND ".join(filters)
            + " ORDER BY recency_at_ms DESC, id DESC LIMIT ?",
            parameters,
        ).fetchall()
        page = tuple(parse_chat(row) for row in rows[:limit])

    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = f"{scope}:{last.recency_at_ms}:{last.chat.id}"
    return ChatPage(tuple(item.chat for item in page), next_cursor)


def visible_text(text: object) -> VisibleText | None:
    if not isinstance(text, str) or not text.strip():
        return None
    return VisibleText(text[:MAX_TEXT_CHARS], len(text) > MAX_TEXT_CHARS)


def user_text(content: object) -> Iterator[str]:
    if not isinstance(content, list):
        return
    entries: list[object] = content
    for part in entries:
        if isinstance(part, dict):
            fields: dict[object, object] = part
            text = fields.get("text")
            if fields.get("type") == "text" and isinstance(text, str):
                yield text


def parse_phase(value: object) -> AnswerPhase | None:
    if isinstance(value, str):
        try:
            return AnswerPhase(value)
        except ValueError:
            pass
    return None


def parse_record(row: sqlite3.Row) -> ChatMessage | None:
    payload: object = row["item_json"]
    kind: object = row["item_type"]
    if not isinstance(payload, str):
        return None
    try:
        decoded: object = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    if not isinstance(decoded, dict):
        return None
    item: dict[object, object] = decoded
    if item.get("type") != kind:
        return None
    match kind:
        case "userMessage":
            preview = visible_text("\n".join(user_text(item.get("content"))))
            return UserMessage(preview) if preview is not None else None
        case "agentMessage":
            preview = visible_text(item.get("text"))
            return AssistantMessage(preview, parse_phase(item.get("phase"))) if preview is not None else None
        case "functionCallOutput":
            message = parse_peer_output(item.get("output"), item.get("namespace"), item.get("name"))
            if message is not None:
                preview = visible_text(message.body)
                if preview is not None:
                    return PeerOutput(preview, message)
    return None


def parse_turn(row: sqlite3.Row) -> StoredTurn:
    return StoredTurn(
        text_column(row["turn_id"]), integer_column(row["rollout_ordinal"]), text_column(row["status"]),
        optional_integer_column(row["started_at"]), optional_integer_column(row["completed_at"]),
    )


def visible_messages(connection: sqlite3.Connection, chat_id: str, turn: StoredTurn) -> Turn:
    rows = connection.execute(
        """
        SELECT item_type, CASE WHEN length(item_json) <= ? THEN item_json END AS item_json
        FROM thread_items
        WHERE thread_id = ? AND turn_id = ? AND (
            item_type IN ('userMessage', 'agentMessage')
            OR CASE WHEN item_type = 'functionCallOutput' AND length(item_json) <= ? AND json_valid(item_json)
                THEN json_extract(item_json, '$.namespace') IN (?, ?)
                    AND json_extract(item_json, '$.name') = ?
                ELSE 0 END
        )
        ORDER BY rollout_ordinal DESC
        LIMIT ?
        """,
        (MAX_RECORD_CHARS, chat_id, turn.id, MAX_RECORD_CHARS, NAMESPACE, LEGACY_NAMESPACE, TOOL_NAME,
         MAX_MESSAGES_PER_TURN + 1),
    ).fetchall()
    parsed = (parse_record(row) for row in reversed(rows[:MAX_MESSAGES_PER_TURN]))
    messages = tuple(message for message in parsed if message is not None)
    return Turn(turn, messages, len(messages) < len(rows))


def read_chat(
    chat_id: str, *, limit: int = 8, cursor: str | None = None,
    include_archived: bool = False, root: Path | None = None,
) -> ChatHistory:
    """Read a page of user, assistant, and bridged peer messages by exact UUID."""
    chat_id = chat_identifier(chat_id)
    validate_limit(limit, MAX_TURNS)
    older_than = None
    if cursor is not None:
        scope, _, sequence = cursor.partition(":")
        if len(cursor) > 80 or scope != chat_id:
            raise HistoryError("Use a history cursor from the same chat")
        older_than = ordinal(sequence)

    with database(root, STATE_DATABASE) as connection:
        row = connection.execute(
            f"SELECT {CHAT_COLUMNS} FROM threads WHERE id = ? AND {DESKTOP_CHAT}", (chat_id,),
        ).fetchone()
        if row is None:
            raise HistoryError("No matching local Codex Desktop chat in the selected archive scope")
        selected = parse_chat(row)
    if selected.chat.archived and not include_archived:
        raise HistoryError("No matching local Codex Desktop chat in the selected archive scope")
    if selected.history_mode != "paginated":
        raise HistoryError("This chat has no supported paginated local history")

    path_filter = "AND rollout_ordinal < ?" if older_than is not None else ""
    parameters = (chat_id, limit + 1) if older_than is None else (chat_id, older_than, limit + 1)
    with database(root, HISTORY_DATABASE) as connection:
        rows = connection.execute(
            "SELECT turn_id, rollout_ordinal, status, started_at, completed_at "
            f"FROM thread_turns WHERE thread_id = ? {path_filter} ORDER BY rollout_ordinal DESC LIMIT ?",
            parameters,
        ).fetchall()
        selected_turns = tuple(parse_turn(row) for row in reversed(rows[:limit]))
        turns = tuple(visible_messages(connection, chat_id, turn) for turn in selected_turns)

    next_cursor = f"{chat_id}:{selected_turns[0].ordinal}" if len(rows) > limit else None
    return ChatHistory(selected.chat, turns, next_cursor)

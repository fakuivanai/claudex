"""Read Codex Desktop 26.1103.60933's local metadata and history projection."""

from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from enum import Enum
import hashlib
from pathlib import Path
import sqlite3
import time
from typing import Annotated, Literal, assert_never

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, RootModel, ValidationError

from claudex.codex.envelope import NAMESPACE, TOOL_NAME, parse_peer_output
from claudex.models import CodexChatId, PeerMessage, Provider, parse_codex_chat_id

# Keep metadata pages small enough for an MCP tool result.
MAX_LIST = 50
# The default metadata page leaves room for additional tool results.
DEFAULT_CHAT_PAGE = 20
# Bound how many turns can expand into messages in one result.
MAX_TURNS = 10
# Most chats fit a useful amount of recent context in this many turns.
DEFAULT_TURN_PAGE = 8
# Reserve room for the original request and keep recent visible entries.
MAX_MESSAGES_PER_TURN = 8
# Bound JSON parsing and avoid reading oversized tool records into Python.
MAX_RECORD_CHARS = 256 * 1024
# Quote a useful text preview without passing every long reply through MCP.
MAX_TEXT_CHARS = 2000
# The title filter case-folds each candidate in SQLite.
MAX_QUERY_CHARS = 160
# Let a busy desktop database win over a long-running history request.
SQL_TIMEOUT = 1.5
# Give SQLite's writer a brief chance to release its lock.
SQL_LOCK_TIMEOUT = 0.3
# Check the read deadline every 1,000 SQLite instructions to limit callback overhead.
SQL_PROGRESS_INTERVAL = 1000
# Cursor ordinals are SQLite signed integers.
SQLITE_MAX_INT = 2**63 - 1
SQLITE_MAX_INT_DIGITS = len(str(SQLITE_MAX_INT))
# A compact query fingerprint keeps cursors tied to their non-secret search scope.
CURSOR_SCOPE_CHARS = 12
# Names from Codex Desktop's local projection and metadata stores.
STATE_DATABASE = "state_5.sqlite"
HISTORY_DATABASE = "thread_history_1.sqlite"
# Match top-level Desktop chats, including those created from a phone.
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
# Keep title previews at the same size in list, search, and index results.
TITLE_PREVIEW_CHARS = 256
CHAT_COLUMNS = f"""
    id, substr(name, 1, {TITLE_PREVIEW_CHARS}) AS name,
    substr(title, 1, {TITLE_PREVIEW_CHARS}) AS title, archived, updated_at_ms, recency_at_ms, history_mode
"""
CheckedChatId = Annotated[CodexChatId, BeforeValidator(parse_codex_chat_id)]
type HistoryMode = Literal["paginated", "legacy"]
type TurnStatus = Literal["completed", "failed", "inProgress", "interrupted"]


class HistoryError(ValueError):
    pass


@dataclass(frozen=True)
class Chat:
    id: CodexChatId
    title: str
    updated_at_ms: int | None
    archived: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "provider": Provider.CODEX.value,
            "id": self.id,
            "title": self.title,
            "updated_at_ms": self.updated_at_ms,
            "archived": self.archived,
        }


@dataclass(frozen=True)
class StoredChat:
    chat: Chat
    recency_at_ms: int
    history_mode: HistoryMode


class DatabaseRecord(BaseModel):
    model_config = ConfigDict(strict=True)


class ChatRow(DatabaseRecord):
    id: CheckedChatId
    name: str | None
    title: str | None
    archived: Literal[0, 1]
    updated_at_ms: int | None
    recency_at_ms: int
    history_mode: HistoryMode


class IndexedChat(DatabaseRecord):
    id: CheckedChatId
    title: str = Field(validation_alias="thread_name")


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


class StoredTurn(DatabaseRecord):
    id: str = Field(validation_alias="turn_id")
    ordinal: int = Field(validation_alias="rollout_ordinal")
    status: TurnStatus
    started_at: int | None
    completed_at: int | None


@dataclass(frozen=True)
class Turn:
    record: StoredTurn
    messages: tuple[ChatMessage, ...]
    over_message_cap: bool
    skipped_records: int

    def to_wire(self) -> dict[str, object]:
        return {
            "id": self.record.id,
            "status": self.record.status,
            "started_at": self.record.started_at,
            "completed_at": self.record.completed_at,
            "messages": [message.to_wire() for message in self.messages],
            "over_message_cap": self.over_message_cap,
            "skipped_records": self.skipped_records,
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


def ordinal(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > SQLITE_MAX_INT_DIGITS:
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
        with closing(sqlite3.connect(
            path.absolute().as_uri() + "?mode=ro", uri=True, timeout=SQL_LOCK_TIMEOUT,
        )) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            deadline = time.monotonic() + SQL_TIMEOUT
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), SQL_PROGRESS_INTERVAL)
            yield connection
    except sqlite3.Error as error:
        if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            raise HistoryError("The local Codex history query exceeded its time limit") from error
        raise HistoryError("The local Codex Desktop history database is unavailable or incompatible") from error


def parse_chat(row: sqlite3.Row) -> StoredChat:
    try:
        data = ChatRow.model_validate(dict(row))
    except ValidationError as error:
        raise HistoryError("The local Codex history contains an invalid chat record") from error
    chat = Chat(data.id, data.name or data.title or data.id, data.updated_at_ms, bool(data.archived))
    return StoredChat(chat, data.recency_at_ms, data.history_mode)


def parse_index_entry(line: str) -> IndexedChat | None:
    try:
        entry = IndexedChat.model_validate_json(line)
    except ValidationError:
        return None
    return entry if entry.title.strip() else None


def chat_title(chat_id: CodexChatId, *, root: Path | None = None) -> str:
    """Resolve a display title from the session index, including CLI chats."""
    index = codex_home(root) / "session_index.jsonl"
    title: str = chat_id
    try:
        with index.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                entry = parse_index_entry(line)
                if entry is not None and entry.id == chat_id:
                    title = entry.title[:TITLE_PREVIEW_CHARS]
    except OSError:
        pass
    return title


def contains_title(value: object, query: str) -> int:
    return int(isinstance(value, str) and query in value.casefold())


def list_chats(
    query: str | None = None, limit: int = DEFAULT_CHAT_PAGE, cursor: str | None = None, archived: bool = False,
    *, root: Path | None = None,
) -> ChatPage:
    """List local top-level Codex Desktop chats using title or exact ID."""
    keyword = "" if query is None else query.strip().casefold()
    scope = hashlib.sha256(f"{int(archived)}:{keyword}".encode("utf-8")).hexdigest()[:CURSOR_SCOPE_CHARS]

    filters = [DESKTOP_CHAT, "archived = ?"]
    parameters: list[str | int] = [archived]
    if keyword:
        try:
            exact = parse_codex_chat_id(keyword)
        except ValueError:
            filters.append(
                f"(claudex_has_title(substr(name, 1, {TITLE_PREVIEW_CHARS}), ?) "
                f"OR claudex_has_title(substr(title, 1, {TITLE_PREVIEW_CHARS}), ?))"
            )
            parameters.extend((keyword, keyword))
        else:
            filters.append("id = ?")
            parameters.append(exact)
    if cursor is not None:
        current_scope, separator, position = cursor.partition(":")
        if not separator or current_scope != scope:
            raise HistoryError("Use a chat list cursor from the same query and archive selection")
        timestamp, _, last_id = position.partition(":")
        filters.append("(recency_at_ms, id) < (?, ?)")
        try:
            identifier = parse_codex_chat_id(last_id)
        except ValueError as error:
            raise HistoryError("Invalid chat list cursor") from error
        parameters.extend((ordinal(timestamp), identifier))
    parameters.append(limit + 1)
    with database(root, STATE_DATABASE) as connection:
        connection.create_function("claudex_has_title", 2, contains_title, deterministic=True)
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


class UserTextPart(DatabaseRecord):
    type: Literal["text"]
    text: str


class NonTextPart(DatabaseRecord):
    type: str


class ProjectedUser(DatabaseRecord):
    type: Literal["userMessage"]
    content: list[UserTextPart | NonTextPart]


class ProjectedAssistant(DatabaseRecord):
    type: Literal["agentMessage"]
    phase: str | None = None
    text: str


class ProjectedToolOutput(DatabaseRecord):
    type: Literal["functionCallOutput"]
    namespace: str
    name: str
    output: str


class ItemRow(DatabaseRecord):
    item_type: str
    item_json: str | None
    rollout_ordinal: int


type ProjectedMessage = Annotated[
    ProjectedUser | ProjectedAssistant | ProjectedToolOutput, Field(discriminator="type")
]


class ProjectedMessageRecord(RootModel[ProjectedMessage]):
    pass


def visible_text(text: str) -> VisibleText | None:
    if not text.strip():
        return None
    return VisibleText(text[:MAX_TEXT_CHARS], len(text) > MAX_TEXT_CHARS)


def parse_phase(phase: str | None) -> AnswerPhase | None:
    try:
        return AnswerPhase(phase)
    except ValueError:
        return None


def parse_record(row: ItemRow) -> ChatMessage | None:
    if row.item_json is None:
        return None
    try:
        record = ProjectedMessageRecord.model_validate_json(row.item_json).root
    except ValidationError:
        return None
    if record.type != row.item_type:
        return None
    match record:
        case ProjectedUser(content=content):
            preview = visible_text("\n".join(part.text for part in content if isinstance(part, UserTextPart)))
            return UserMessage(preview) if preview is not None else None
        case ProjectedAssistant(text=text, phase=phase):
            preview = visible_text(text)
            return AssistantMessage(preview, parse_phase(phase)) if preview is not None else None
        case ProjectedToolOutput(output=output, namespace=namespace, name=name):
            message = parse_peer_output(output, namespace, name)
            if message is None or (preview := visible_text(message.body)) is None:
                return None
            return PeerOutput(preview, message)
        case _:
            assert_never(record)


def parse_turn(row: sqlite3.Row) -> StoredTurn:
    try:
        return StoredTurn.model_validate(dict(row))
    except ValidationError as error:
        raise HistoryError("The local Codex history contains an invalid turn record") from error


def visible_messages(connection: sqlite3.Connection, chat_id: CodexChatId, turn: StoredTurn) -> Turn:
    first_user = connection.execute(
        """
        SELECT item_type, rollout_ordinal, CASE WHEN length(item_json) <= ? THEN item_json END AS item_json
        FROM thread_items WHERE thread_id = ? AND turn_id = ? AND item_type = 'userMessage'
        ORDER BY rollout_ordinal LIMIT 1
        """,
        (MAX_RECORD_CHARS, chat_id, turn.id),
    ).fetchone()
    original_ordinal = None
    messages: list[tuple[int, ChatMessage]] = []
    skipped = 0
    if first_user is not None:
        try:
            first = ItemRow.model_validate(dict(first_user))
        except ValidationError:
            # The recent-record scan still includes this unpinned row.
            pass
        else:
            original_ordinal = first.rollout_ordinal
            original = parse_record(first)
            if original is None:
                skipped += 1
            else:
                messages.append((original_ordinal, original))

    # Fetch newest records first, filling the visible budget after skipping bad records.
    rows = connection.execute(
        """
        SELECT item_type, rollout_ordinal, CASE WHEN length(item_json) <= ? THEN item_json END AS item_json
        FROM thread_items
        WHERE thread_id = ? AND turn_id = ? AND (? IS NULL OR rollout_ordinal != ?) AND (
            item_type IN ('userMessage', 'agentMessage')
            OR CASE WHEN item_type = 'functionCallOutput' AND length(item_json) <= ? AND json_valid(item_json)
                THEN json_extract(item_json, '$.namespace') = ?
                    AND json_extract(item_json, '$.name') = ?
                ELSE 0 END
        )
        ORDER BY rollout_ordinal DESC
        """,
        (MAX_RECORD_CHARS, chat_id, turn.id, original_ordinal, original_ordinal, MAX_RECORD_CHARS, NAMESPACE, TOOL_NAME),
    )
    over_cap = False
    with closing(rows):
        for row in rows:
            try:
                item = ItemRow.model_validate(dict(row))
            except ValidationError:
                skipped += 1
                continue
            message = parse_record(item)
            if message is None:
                skipped += 1
            elif len(messages) == MAX_MESSAGES_PER_TURN:
                over_cap = True
                break
            else:
                messages.append((item.rollout_ordinal, message))
    return Turn(turn, tuple(message for _, message in sorted(messages, key=lambda item: item[0])), over_cap, skipped)


def read_chat(
    chat_id: CodexChatId, *, limit: int = DEFAULT_TURN_PAGE, cursor: str | None = None,
    include_archived: bool = False, root: Path | None = None,
) -> ChatHistory:
    """Read a page of user, assistant, and bridged peer messages by exact UUID."""
    older_than = None
    if cursor is not None:
        scope, _, sequence = cursor.partition(":")
        if scope != chat_id:
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

"""Discover Claude Desktop chats and submit native inbox notices on Linux."""

from dataclasses import dataclass
from enum import StrEnum
import json
import logging
import os
from pathlib import Path
import socket
from typing import Annotated, Literal, TypeVar

from claude_agent_sdk import SessionMessage, get_session_messages
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter, ValidationError

from claudex.models import ClaudeChatId, Sender, UUIDText, parse_claude_chat_id, parse_uuid

# Keep the title short inside the native one-line mailbox notice.
MAX_NOTICE_TITLE = 120
# Bound the visible local transcript preview returned by one MCP call.
MAX_HISTORY_MESSAGES = 100
# A single transcript entry may include a much larger pasted document.
MAX_HISTORY_TEXT = 4000
# Keep external titles literal inside Claude Code's XML-like peer message wrapper.
NOTICE_ESCAPES = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", "[": "\\u005b", "]": "\\u005d"})

DesktopId = Annotated[ClaudeChatId, BeforeValidator(parse_claude_chat_id)]
MACHINE_ID_PATH = Path("/etc/machine-id")
LOGGER = logging.getLogger(__name__)


class ClaudeDesktopError(RuntimeError):
    pass


class ClaudeRecordError(ClaudeDesktopError):
    """A native session record could not be read or did not match the pinned format."""


class Liveness(StrEnum):
    PROCESS_VISIBLE = "process_visible"
    INBOX_PRESENT = "inbox_present"
    SAVED = "saved"


class WorkerStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WORKING = "working"
    BUSY = "busy"
    WAITING = "waiting"


def worker_status(value: object) -> WorkerStatus | None:
    if isinstance(value, str):
        try:
            return WorkerStatus(value)
        except ValueError:
            pass
    return None


class NoticeStatus(StrEnum):
    SUBMITTED = "submitted_unconfirmed"
    UNCERTAIN = "unknown"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class ClaudeChat:
    id: ClaudeChatId
    title: str
    status: WorkerStatus | None
    liveness: Liveness


@dataclass(frozen=True)
class LiveChat:
    chat: ClaudeChat
    backend_id: str
    inbox: Path | None


@dataclass(frozen=True)
class HistoryMessage:
    role: MessageRole
    text: str


class TranscriptContent(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)
    content: str | list[object]


class TranscriptText(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)
    type: Literal["text"]
    text: str


@dataclass(frozen=True)
class ClaudeHistory:
    chat: ClaudeChat
    messages: tuple[HistoryMessage, ...]


class SavedChat(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    id: Annotated[DesktopId, Field(alias="sessionId")]
    archived: Annotated[bool, Field(alias="isArchived")]
    backend_id: Annotated[UUIDText | None, Field(alias="cliSessionId")] = None
    archived_backend_id: Annotated[UUIDText | None, Field(alias="unarchivedCliSessionId")] = None
    title: str | None = None


class RuntimeMarker(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    entrypoint: Literal["claude-desktop"]
    kind: Literal["interactive"]
    id: Annotated[DesktopId, Field(alias="hostSessionId")]
    backend_id: Annotated[UUIDText, Field(alias="sessionId")]
    process_id: Annotated[int, Field(alias="pid", gt=0)]
    process_domain: Annotated[str, Field(alias="pidDomain")]
    inbox_path: Annotated[str | None, Field(alias="messagingSocketPath")] = None
    status: Annotated[WorkerStatus | None, BeforeValidator(worker_status)] = None
    name: str | None = None


RECORD = TypeAdapter(dict[str, object])
Record = TypeVar("Record", SavedChat, RuntimeMarker)


def read_record(path: Path) -> dict[str, object] | None:
    try:
        return RECORD.validate_json(path.read_bytes(), strict=True)
    except FileNotFoundError:
        # Claude can remove a worker marker between the directory scan and read.
        return None
    except OSError as error:
        raise ClaudeRecordError(f"Could not read Claude record {path.name}") from error
    except ValidationError:
        raise ClaudeRecordError(f"Claude record {path.name} is not a valid JSON object") from None


def parse_record(path: Path, raw: dict[str, object], schema: type[Record]) -> Record:
    try:
        return schema.model_validate(raw)
    except ValidationError:
        raise ClaudeRecordError(f"Claude record {path.name} does not match the supported {schema.__name__} format") from None


def saved_records(target: ClaudeChatId, *, include_archived: bool = False) -> list[SavedChat]:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    registry = config / "Claude" / "claude-code-sessions"
    records: list[SavedChat] = []
    for path in registry.glob(f"*/*/{target}.json"):
        raw = read_record(path)
        if raw is None:
            continue
        record = parse_record(path, raw, SavedChat)
        if record.id != target:
            raise ClaudeRecordError(f"Claude record {path.name} contains a different chat ID")
        if include_archived or not record.archived:
            records.append(record)
    return records


def _live_title(marker: RuntimeMarker) -> str | None:
    titles = {
        record.title
        for record in saved_records(marker.id)
        if record.backend_id == marker.backend_id and record.title and record.title.strip()
    }
    if len(titles) == 1:
        return titles.pop()
    return marker.name if marker.name in titles else None


def _matching_chat(path: Path, machine: str, namespace: str) -> LiveChat | None:
    raw = read_record(path)
    if raw is None or raw.get("entrypoint") != "claude-desktop" or raw.get("kind") != "interactive":
        return None
    marker = parse_record(path, raw, RuntimeMarker)
    if path.stem != str(marker.process_id):
        raise ClaudeRecordError(f"Claude worker record {path.name} contains a different process ID")
    host_domain = f"linux:{machine}:"
    if not marker.process_domain.startswith(host_domain):
        return None
    process_visible = marker.process_domain == host_domain + namespace
    if process_visible and not Path(f"/proc/{marker.process_id}").is_dir():
        return None
    inbox = Path(marker.inbox_path) if marker.inbox_path else None
    if inbox is not None and not (inbox.is_absolute() and inbox.is_socket()):
        inbox = None
    if not process_visible and inbox is None:
        return None
    title = _live_title(marker)
    if title is None:
        return None
    liveness = Liveness.PROCESS_VISIBLE if process_visible else Liveness.INBOX_PRESENT
    return LiveChat(ClaudeChat(marker.id, title, marker.status, liveness), marker.backend_id, inbox)


def live_sessions() -> list[LiveChat]:
    code = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        machine = MACHINE_ID_PATH.read_text(encoding="ascii").strip()
        namespace = os.readlink("/proc/self/ns/pid")
    except OSError as error:
        raise ClaudeDesktopError("Claude Desktop discovery needs the local Linux machine ID and PID namespace") from error

    matches: dict[ClaudeChatId, list[LiveChat]] = {}
    for path in (code / "sessions").glob("*.json"):
        try:
            live = _matching_chat(path, machine, namespace)
        except ClaudeRecordError as error:
            LOGGER.warning("%s", error)
            continue
        if live is not None:
            matches.setdefault(live.chat.id, []).append(live)
    return [sessions[0] for sessions in matches.values() if len(sessions) == 1]


def sender() -> ClaudeChat:
    # Claude Code provides the startup UUID to each stdio MCP process:
    # https://code.claude.com/docs/en/env-vars
    try:
        backend_id = parse_uuid(os.environ.get("CLAUDE_CODE_SESSION_ID"))
    except ValueError:
        raise ClaudeDesktopError("Claude Code did not provide a session ID to this MCP process") from None
    matching = [live.chat for live in live_sessions() if live.backend_id == backend_id]
    if len(matching) != 1:
        raise ClaudeDesktopError("Claude Code's MCP session ID no longer identifies one live Desktop chat. Reconnect the MCP server")
    return matching[0]


def _visible_text(content: str | list[object]) -> str:
    if isinstance(content, str):
        return content[:MAX_HISTORY_TEXT]
    text: list[str] = []
    for part in content:
        try:
            text.append(TranscriptText.model_validate(part).text[:MAX_HISTORY_TEXT])
        except ValidationError:
            continue
    return "\n".join(text)[:MAX_HISTORY_TEXT]


def _history_message(row: SessionMessage) -> HistoryMessage | None:
    try:
        message = TranscriptContent.model_validate(row.message)
    except ValidationError:
        return None
    return HistoryMessage(MessageRole(row.type), _visible_text(message.content))


def read_chat(target: ClaudeChatId, limit: int, *, include_archived: bool = False) -> ClaudeHistory:
    live = next((session for session in live_sessions() if session.chat.id == target), None)
    if live is not None:
        chat, backend_id = live.chat, live.backend_id
    else:
        records = saved_records(target, include_archived=include_archived)
        backends = {
            backend for record in records
            if (backend := record.backend_id or record.archived_backend_id) is not None
        }
        if len(backends) != 1:
            raise ValueError("No unique saved Claude Desktop transcript matches this chat.")
        backend_id = backends.pop()
        titles = {record.title for record in records if record.title}
        title = titles.pop() if len(titles) == 1 else str(target)
        chat = ClaudeChat(target, title, None, Liveness.SAVED)
    # The SDK resolves a project from the exact UUID and reconstructs the current branch.
    # Its limit starts at the beginning, so apply our window from the end instead.
    rows = get_session_messages(backend_id)
    messages = tuple(
        message for row in rows[-limit:]
        if row.session_id == backend_id and (message := _history_message(row)) is not None
    )
    return ClaudeHistory(chat, messages)


def notice_text(message_id: str, sender: Sender) -> str:
    prefix = f"Claudex mailbox message {message_id} is available"
    title = " ".join("".join(char if char.isprintable() else " " for char in sender.title).split())
    if not title or title == sender.chat_id:
        return f"{prefix} from Codex chat {sender.chat_id}."
    if len(title) > MAX_NOTICE_TITLE:
        title = title[:MAX_NOTICE_TITLE - 1].rstrip() + "…"
    quoted_title = json.dumps(title, ensure_ascii=False).translate(NOTICE_ESCAPES)
    return f"{prefix} from Codex chat {quoted_title} (ID {sender.chat_id})."


def submit_notice(target: LiveChat, message_id: str, sender: Sender) -> NoticeStatus:
    if target.inbox is None:
        raise ClaudeDesktopError("Claude Desktop chat has no matching live inbox")
    # Claude Code 2.1.325 interprets this frame as an attributed peer notice
    # under its own inbound and permission controls.
    # https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket
    frame = {
        "type": "user",
        "session_id": target.backend_id,
        "from": "claudex",
        "priority": "next",
        "message": {"role": "user", "content": notice_text(message_id, sender)},
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        # A local inbox should accept promptly. A stalled worker leaves the body in the mailbox.
        connection.settimeout(2)
        try:
            connection.connect(str(target.inbox))
        except OSError as error:
            raise ClaudeDesktopError("Could not open the Claude Desktop inbox") from error
        try:
            connection.sendall((json.dumps(frame) + "\n").encode("utf-8"))
        except OSError:
            return NoticeStatus.UNCERTAIN
    return NoticeStatus.SUBMITTED

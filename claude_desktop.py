"""Discover Claude Desktop chats and submit native inbox notices on Linux."""

from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path
import re
import socket
from typing import Annotated, Literal, NewType, TypeVar

from claude_agent_sdk import SessionMessage, get_session_messages
from pydantic import Field, TypeAdapter, ValidationError


UUID_PATTERN = r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"
CHAT_PATTERN = "^local_" + UUID_PATTERN[1:]
MESSAGE_PATTERN = r"[A-Za-z0-9_-]+"
MAX_NOTICE_TITLE = 120
MAX_HISTORY_MESSAGES = 100
MAX_HISTORY_TEXT = 4000
NOTICE_ESCAPES = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", "[": "\\u005b", "]": "\\u005d"})

ClaudeChatId = NewType("ClaudeChatId", str)
BackendId = Annotated[str, Field(pattern=UUID_PATTERN)]
DesktopId = Annotated[ClaudeChatId, Field(pattern=CHAT_PATTERN)]


class ClaudeDesktopError(RuntimeError):
    pass


class Liveness(StrEnum):
    PROCESS_VISIBLE = "process_visible"
    INBOX_PRESENT = "inbox_present"


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
    status: str
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


@dataclass(frozen=True)
class ClaudeHistory:
    chat: ClaudeChat
    messages: tuple[HistoryMessage, ...]


@dataclass(frozen=True)
class CodexNoticeSender:
    id: str
    title: str


@dataclass(frozen=True)
class SavedChat:
    id: Annotated[DesktopId, Field(alias="sessionId")]
    archived: Annotated[bool, Field(alias="isArchived")]
    backend_id: Annotated[BackendId | None, Field(alias="cliSessionId")] = None
    title: str | None = None


@dataclass(frozen=True)
class RuntimeMarker:
    entrypoint: Literal["claude-desktop"]
    kind: Literal["interactive"]
    id: Annotated[DesktopId, Field(alias="hostSessionId")]
    backend_id: Annotated[BackendId, Field(alias="sessionId")]
    process_id: Annotated[int, Field(alias="pid", gt=0)]
    process_domain: Annotated[str, Field(alias="pidDomain")]
    inbox_path: Annotated[str | None, Field(alias="messagingSocketPath")] = None
    status: str | None = None
    name: str | None = None


SAVED_CHAT = TypeAdapter(SavedChat)
RUNTIME_MARKER = TypeAdapter(RuntimeMarker)
Record = TypeVar("Record", SavedChat, RuntimeMarker)


def parse_chat_identifier(value: str) -> ClaudeChatId:
    if not re.fullmatch(CHAT_PATTERN, value):
        raise ValueError("Use the exact local Claude Desktop chat ID.")
    return ClaudeChatId(value)


def read_record(path: Path, schema: TypeAdapter[Record]) -> Record | None:
    try:
        return schema.validate_json(path.read_bytes(), strict=True)
    except (OSError, ValidationError):
        return None


def saved_records(target: ClaudeChatId) -> list[SavedChat]:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    registry = config / "Claude" / "claude-code-sessions"
    records: list[SavedChat] = []
    for path in registry.glob(f"*/*/{target}.json"):
        record = read_record(path, SAVED_CHAT)
        if record is not None and record.id == target and not record.archived:
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
    marker = read_record(path, RUNTIME_MARKER)
    if marker is None or path.stem != str(marker.process_id):
        return None
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
    return LiveChat(ClaudeChat(marker.id, title, marker.status or "unknown", liveness), marker.backend_id, inbox)


def live_sessions() -> list[LiveChat]:
    code = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        machine = Path("/etc/machine-id").read_text(encoding="ascii").strip()
        namespace = os.readlink("/proc/self/ns/pid")
    except OSError as error:
        raise ClaudeDesktopError("Claude Desktop discovery needs the local Linux machine ID and PID namespace") from error

    matches: dict[ClaudeChatId, list[LiveChat]] = {}
    for path in (code / "sessions").glob("*.json"):
        live = _matching_chat(path, machine, namespace)
        if live is not None:
            matches.setdefault(live.chat.id, []).append(live)
    return [sessions[0] for sessions in matches.values() if len(sessions) == 1]


def list_chats() -> list[ClaudeChat]:
    return sorted((live.chat for live in live_sessions()), key=lambda chat: (chat.title.casefold(), chat.id))


def sender() -> ClaudeChat:
    # Claude Code provides the startup UUID to each stdio MCP process:
    # https://code.claude.com/docs/en/env-vars
    backend_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not re.fullmatch(UUID_PATTERN, backend_id):
        raise ClaudeDesktopError("Claude Code did not provide a session ID to this MCP process")
    matching = [live.chat for live in live_sessions() if live.backend_id == backend_id]
    if len(matching) != 1:
        raise ClaudeDesktopError("Claude Code's MCP session ID no longer identifies one live Desktop chat. Reconnect the MCP server")
    return matching[0]


def _visible_text(content: object) -> str:
    if isinstance(content, str):
        return content[:MAX_HISTORY_TEXT]
    if not isinstance(content, list):
        return ""
    text: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            value: object = part.get("text")
            if isinstance(value, str):
                text.append(value[:MAX_HISTORY_TEXT])
    return "\n".join(text)[:MAX_HISTORY_TEXT]


def _history_message(row: SessionMessage) -> HistoryMessage | None:
    message: object = row.message
    if not isinstance(message, dict):
        return None
    return HistoryMessage(MessageRole(row.type), _visible_text(message.get("content")))


def read_chat(target: ClaudeChatId, limit: int = 20) -> ClaudeHistory:
    if not 1 <= limit <= MAX_HISTORY_MESSAGES:
        raise ValueError(f"Use a Claude history limit from 1 to {MAX_HISTORY_MESSAGES}.")
    live = next((session for session in live_sessions() if session.chat.id == target), None)
    if live is None:
        raise ValueError("Open the exact Claude Code Desktop chat before reading it.")
    # The SDK resolves a project from the exact UUID and reconstructs the current branch.
    # Its limit starts at the beginning, so apply our window from the end instead.
    rows = get_session_messages(live.backend_id)
    messages = tuple(
        message for row in rows[-limit:]
        if row.session_id == live.backend_id and (message := _history_message(row)) is not None
    )
    return ClaudeHistory(live.chat, messages)


def notice_text(message_id: str, sender: CodexNoticeSender | None) -> str:
    if not re.fullmatch(MESSAGE_PATTERN, message_id):
        raise ClaudeDesktopError("Expected a mailbox message ID")
    prefix = f"Claudex mailbox message {message_id} is available"
    if sender is None:
        return f"{prefix}."
    if not re.fullmatch(UUID_PATTERN, sender.id):
        raise ClaudeDesktopError("Expected an exact Codex Desktop sender chat ID")
    title = " ".join("".join(char if char.isprintable() else " " for char in sender.title).split())
    if not title or title == sender.id:
        return f"{prefix} from Codex chat {sender.id}."
    if len(title) > MAX_NOTICE_TITLE:
        title = title[:MAX_NOTICE_TITLE - 1].rstrip() + "…"
    quoted_title = json.dumps(title, ensure_ascii=False).translate(NOTICE_ESCAPES)
    return f"{prefix} from Codex chat {quoted_title} (ID {sender.id})."


def submit_notice(target: LiveChat, message_id: str, sender: CodexNoticeSender | None) -> NoticeStatus:
    if target.inbox is None:
        raise ClaudeDesktopError("Claude Desktop chat has no matching live inbox")
    # Claude Code 2.1.325 interprets this frame as an attributed peer notice
    # under its own inbound and permission controls.
    # https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket
    frame = {
        "type": "user",
        "session_id": target.backend_id,
        "from": "thread-bridge",
        "priority": "next",
        "message": {"role": "user", "content": notice_text(message_id, sender)},
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
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

"""Launch the signed-in Claude Desktop and open native Code links."""

from dataclasses import dataclass, field
from enum import StrEnum
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.parse import urlencode

import claude_desktop
from claude_desktop import ClaudeChatId, ClaudeDesktopError, CodexNoticeSender, LiveChat, NoticeStatus


OPEN_TIMEOUT = 18.0


class LaunchStatus(StrEnum):
    REQUESTED = "launch_requested"


class DraftStatus(StrEnum):
    REQUESTED = "draft_requested"


class WakeStatus(StrEnum):
    ALREADY_RUNNING = "already_running"
    INBOX_READY = "inbox_ready"
    WAITING_FOR_INBOX = "waiting_for_inbox"


class StoredStatus(StrEnum):
    STORED = "stored"


@dataclass(frozen=True)
class WakeResult:
    chat_id: ClaudeChatId
    status: WakeStatus


@dataclass(frozen=True)
class StoredNotice:
    message_id: str
    reason: str
    status: StoredStatus = field(default=StoredStatus.STORED, init=False)
    opened_saved_chat: bool = field(default=False, init=False)


@dataclass(frozen=True)
class AttemptedNotice:
    message_id: str
    status: NoticeStatus
    opened_saved_chat: bool


NoticeResult = StoredNotice | AttemptedNotice


@dataclass(frozen=True)
class _ReadyInbox:
    live: LiveChat
    opened_saved_chat: bool


def _launcher() -> Path:
    configured = os.environ.get("CLAUDEX_CLAUDE_LAUNCHER")
    default = Path.home() / ".local" / "bin" / "claude-desktop"
    if configured:
        candidate = Path(configured)
    else:
        fallback = shutil.which("claude-desktop") if not default.is_file() else None
        candidate = Path(fallback) if fallback else default
    if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ClaudeDesktopError("Set CLAUDEX_CLAUDE_LAUNCHER to the absolute path of your executable Claude Desktop launcher.")
    return candidate


def launch(url: str | None = None) -> LaunchStatus:
    arguments = [str(_launcher())]
    if url is not None:
        arguments.append(url)
    try:
        subprocess.Popen(
            arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise ClaudeDesktopError("Could not launch Claude Desktop") from error
    return LaunchStatus.REQUESTED


def validate_saved(target: ClaudeChatId) -> None:
    if not claude_desktop.saved_records(target):
        raise ValueError("No saved, unarchived Claude Desktop chat matches that ID.")


def wait_for_inbox(target: ClaudeChatId, timeout: float = OPEN_TIMEOUT) -> LiveChat | None:
    deadline = time.monotonic() + timeout
    while True:
        for live in claude_desktop.live_sessions():
            if live.chat.id == target and live.inbox is not None:
                return live
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def draft_url(prompt: str, folder: str | None = None) -> str:
    parameters = {"q": prompt}
    if folder is not None:
        path = Path(folder).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Use an absolute path to an existing local folder.")
        parameters["folder"] = str(path)
    # Native Code draft links populate the composer and leave submission to the person:
    # https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link
    return "claude://code/new?" + urlencode(parameters)


def open_draft(prompt: str, folder: str | None = None) -> DraftStatus:
    launch(draft_url(prompt, folder))
    return DraftStatus.REQUESTED


def _ensure_inbox(target: ClaudeChatId) -> _ReadyInbox | None:
    live = wait_for_inbox(target, timeout=0)
    if live is not None:
        return _ReadyInbox(live, opened_saved_chat=False)
    validate_saved(target)
    # Desktop 5.11492.5 accepts its own local ID here and keeps its own runtime.
    launch("claude://code/continue?" + urlencode({"session": target}))
    live = wait_for_inbox(target)
    return _ReadyInbox(live, opened_saved_chat=True) if live is not None else None


def wake(target: ClaudeChatId) -> WakeResult:
    ready = _ensure_inbox(target)
    if ready is None:
        return WakeResult(target, WakeStatus.WAITING_FOR_INBOX)
    return WakeResult(target, WakeStatus.INBOX_READY if ready.opened_saved_chat else WakeStatus.ALREADY_RUNNING)


def notify(target: ClaudeChatId, message_id: str, *, sender: CodexNoticeSender | None = None) -> NoticeResult:
    """Notify the exact live inbox, or leave the message stored until it appears."""
    ready = _ensure_inbox(target)
    if ready is None:
        return StoredNotice(message_id, "No live inbox appeared after requesting the saved chat.")
    status = claude_desktop.submit_notice(ready.live, message_id, sender)
    return AttemptedNotice(message_id, status, ready.opened_saved_chat)

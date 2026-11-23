"""Launch the signed-in Claude Desktop and open native Code links."""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode

from claudex.claude import chats
from claudex.claude.chats import ClaudeDesktopError, LiveChat, NoticeStatus
from claudex.claude.control import DesktopControl, Success
from claudex.environment import desktop_environment
from claudex.models import ClaudeChatId, Sender


# A native worker can return before publishing its Claude Code inbox.
INBOX_TIMEOUT = 18.0
# Poll the local registry without contending with the worker's startup.
INBOX_POLL_INTERVAL = 0.25


@dataclass(frozen=True)
class StoredNotice:
    reason: str


@dataclass(frozen=True)
class AttemptedNotice:
    status: NoticeStatus
    opened_saved_chat: bool


NoticeResult = StoredNotice | AttemptedNotice


def _launcher() -> Path:
    configured = os.environ.get("CLAUDEX_CLAUDE_LAUNCHER")
    candidate = Path(configured) if configured else None
    if candidate is None or not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ClaudeDesktopError("Set CLAUDEX_CLAUDE_LAUNCHER to the absolute path of your executable Claude Desktop launcher.")
    return candidate


def launch(url: str | None = None) -> None:
    arguments = [str(_launcher())]
    if url is not None:
        arguments.append(url)
    try:
        environment = desktop_environment()
        subprocess.Popen(
            arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=environment["HOME"], env=environment, start_new_session=True,
        )
    except OSError as error:
        raise ClaudeDesktopError("Could not launch Claude Desktop") from error


def wait_for_inbox(target: ClaudeChatId, timeout: float = INBOX_TIMEOUT) -> LiveChat | None:
    deadline = time.monotonic() + timeout
    while True:
        for live in chats.live_sessions():
            if live.chat.id == target and live.inbox is not None:
                return live
        if time.monotonic() >= deadline:
            return None
        time.sleep(INBOX_POLL_INTERVAL)


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


def open_draft(prompt: str, folder: str | None = None) -> None:
    launch(draft_url(prompt, folder))


def notify(target: ClaudeChatId, message_id: str, *, sender: Sender, control: DesktopControl) -> NoticeResult:
    """Notify the exact live inbox, or leave the message stored until it appears."""
    live = wait_for_inbox(target, timeout=0)
    if live is not None:
        return AttemptedNotice(chats.submit_notice(live, message_id, sender), opened_saved_chat=False)

    warmed = control.warm_chat(target)
    if not isinstance(warmed, Success):
        return StoredNotice(warmed.reason)
    if warmed.result.chat is None or not warmed.result.chat.warm:
        return StoredNotice(warmed.result.notice or "Desktop did not start a worker for this chat.")
    live = wait_for_inbox(target)
    if live is None:
        return StoredNotice("The worker did not publish a live inbox in time.")
    return AttemptedNotice(chats.submit_notice(live, message_id, sender), opened_saved_chat=True)

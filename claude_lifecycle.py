"""Launch and reopen exact Desktop-owned Claude Code chats."""
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode

import claude_desktop


OPEN_TIMEOUT = 18.0  # Allow the managed Desktop worker to start before reporting queued work.


def launch(url=None):
    launcher = Path(os.environ.get("THREAD_BRIDGE_CLAUDE_LAUNCHER") or Path.home() / ".local/bin/claude-desktop")
    if not launcher.is_absolute() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise claude_desktop.ClaudeDesktopError("Configure the absolute, executable account-preserving Claude Desktop launcher.")
    args = [str(launcher)]
    if url is not None:
        args.append(url)
    try:
        subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise claude_desktop.ClaudeDesktopError("Could not launch Claude Desktop") from error
    return {"status": "launch_requested"}


def validate_saved(thread_id):
    if not isinstance(thread_id, str) or not claude_desktop.DESKTOP_ID.fullmatch(thread_id):
        raise ValueError("Use the exact local Claude Desktop chat ID.")
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    root = config_root / "Claude/claude-code-sessions"
    for path in root.glob(f"*/*/{thread_id}.json"):
        row = claude_desktop.read_object(path)
        if row.get("sessionId") == thread_id and row.get("isArchived") is False:
            return
    raise ValueError("No saved, unarchived Claude Desktop chat matches that ID.")


def wait_for_box(thread_id, timeout=OPEN_TIMEOUT):
    until = time.monotonic() + timeout
    while True:
        for chat in claude_desktop.live_sessions():
            if chat["id"] == thread_id and chat["inbox"] is not None:
                return chat
        if time.monotonic() >= until:
            return None
        time.sleep(0.25)  # Recheck the registry without busy-spinning during startup.


def draft_url(prompt, folder=None):
    params = {"q": prompt}
    if folder is not None:
        path = Path(folder).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Use an absolute path to an existing local folder.")
        params["folder"] = str(path)
    return "claude://code/new?" + urlencode(params)


def open_draft(prompt, folder=None):
    """Open the documented native Code draft. Desktop handles submission."""
    launch(draft_url(prompt, folder))
    return {"status": "draft_requested"}


def wake(thread_id):
    """Ask Desktop to reopen the exact saved chat and check for its inbox."""
    if wait_for_box(thread_id, timeout=0):
        return {"status": "already_running", "thread_id": thread_id}
    validate_saved(thread_id)
    # Installed Claude Desktop 5.11492.5 also accepts an exact local ID here.
    # Measured against the app's native manager, not the CLI import/resume URI.
    launch("claude://code/continue?" + urlencode({"session": thread_id}))
    status = "inbox_ready" if wait_for_box(thread_id) is not None else "waiting_for_inbox"
    return {"status": status, "thread_id": thread_id}


def notify(thread_id, message_id, *, sender=None):
    """Wake an idle live chat, or ask Desktop to restore the saved chat first."""
    result = wake(thread_id)
    if result["status"] == "waiting_for_inbox":
        return {
            "status": "stored", "message_id": message_id,
            "notification": result.get("reason") or "No live inbox appeared after requesting the saved chat.",
            "opened_saved_chat": False,
        }
    return {
        **claude_desktop.notify(thread_id, message_id, sender=sender),
        "opened_saved_chat": result["status"] == "inbox_ready",
    }

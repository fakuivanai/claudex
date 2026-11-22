"""Resolve live Claude Desktop Code identities and notify their local inboxes."""

from collections import defaultdict
import json
import os
from pathlib import Path
import re
import socket


UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
DESKTOP_ID = re.compile(r"local_" + UUID.pattern)
MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
MAX_NOTICE_TITLE = 120
NOTICE_ESCAPES = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", "[": "\\u005b", "]": "\\u005d"})


class ClaudeDesktopError(RuntimeError):
    pass


def read_object(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def live_sessions():
    config_base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    code_base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    registry = config_base / "Claude" / "claude-code-sessions"
    try:
        machine_id = Path("/etc/machine-id").read_text(encoding="ascii").strip()
        pid_namespace = os.readlink("/proc/self/ns/pid")
    except OSError as error:
        raise ClaudeDesktopError("Claude Desktop inbox discovery needs the local Linux machine ID and PID namespace") from error

    results = defaultdict(list)
    for path in (code_base / "sessions").glob("*.json"):
        marker = read_object(path)
        if marker.get("entrypoint") != "claude-desktop" or marker.get("kind") != "interactive":
            continue
        desktop_id, backend_id = marker.get("hostSessionId"), marker.get("sessionId")
        if not isinstance(desktop_id, str) or not DESKTOP_ID.fullmatch(desktop_id):
            continue
        if not isinstance(backend_id, str) or not UUID.fullmatch(backend_id):
            continue
        pid, domain = marker.get("pid"), marker.get("pidDomain")
        if type(pid) is not int or pid <= 0 or path.stem != str(pid):
            continue
        if not isinstance(domain, str) or not domain.startswith(f"linux:{machine_id}:"):
            continue
        process_visible = domain == f"linux:{machine_id}:{pid_namespace}"
        if process_visible and not Path(f"/proc/{pid}").is_dir():
            continue
        address = marker.get("messagingSocketPath")
        inbox = Path(address) if isinstance(address, str) else None
        if inbox is not None and not (inbox.is_absolute() and inbox.is_socket()):
            inbox = None
        if not process_visible and inbox is None:
            continue

        # A shared registry can expose the same record under multiple account
        # paths. Only the current backend UUID identifies the running Desktop chat.
        titles = set()
        for record_path in registry.glob(f"*/*/{desktop_id}.json"):
            record = read_object(record_path)
            title = record.get("title")
            if (
                record.get("sessionId") == desktop_id and record.get("cliSessionId") == backend_id
                and record.get("isArchived") is False and isinstance(title, str) and title.strip()
            ):
                titles.add(title)
        if len(titles) == 1:
            title = titles.pop()
        elif isinstance(marker.get("name"), str) and marker["name"] in titles:
            title = marker["name"]
        else:
            continue
        status = marker.get("status")
        results[desktop_id].append({
            "provider": "claude", "id": desktop_id, "title": title, "backend_id": backend_id,
            "status": status if isinstance(status, str) else "unknown", "inbox": inbox,
            "liveness": "process_visible" if process_visible else "inbox_present",
        })
    return [matches[0] for matches in results.values() if len(matches) == 1]


def public_chat(chat):
    return {key: chat[key] for key in ("provider", "id", "title", "status", "liveness")}


def list_chats():
    """List running Desktop chats with a registry match and liveness evidence."""
    chats = sorted(live_sessions(), key=lambda chat: (chat["title"].casefold(), chat["id"]))
    return [public_chat(chat) for chat in chats]


def sender():
    # Claude Code provides the startup UUID to each stdio MCP process:
    # https://code.claude.com/docs/en/env-vars
    backend_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not UUID.fullmatch(backend_id):
        raise ClaudeDesktopError("Claude Code did not provide a session ID to this MCP process")
    matches = [chat for chat in live_sessions() if chat["backend_id"] == backend_id]
    if len(matches) != 1:
        raise ClaudeDesktopError("Claude Code's MCP session ID no longer identifies one live Desktop chat. Reconnect the MCP server")
    return public_chat(matches[0])


def notice_text(message_id, sender):
    prefix = f"Thread bridge mailbox message {message_id} is available"
    if sender is None:
        return f"{prefix}."
    if not isinstance(sender, dict) or sender.get("provider") != "codex":
        raise ClaudeDesktopError("Expected a resolved Codex Desktop sender")
    sender_id = sender.get("id")
    if not isinstance(sender_id, str) or not UUID.fullmatch(sender_id):
        raise ClaudeDesktopError("Expected an exact Codex Desktop sender chat ID")
    title = sender.get("title")
    if isinstance(title, str):
        title = " ".join("".join(char if char.isprintable() else " " for char in title).split())
    if not title or not isinstance(title, str) or title == sender_id:
        return f"{prefix} from Codex chat {sender_id}."
    if len(title) > MAX_NOTICE_TITLE:
        title = title[:MAX_NOTICE_TITLE - 1].rstrip() + "…"
    quoted_title = json.dumps(title, ensure_ascii=False).translate(NOTICE_ESCAPES)
    return f"{prefix} from Codex chat {quoted_title} (ID {sender_id})."


def notify(thread_id, message_id, *, sender=None):
    """Submit a fixed notice. The recipient can read the message through MCP."""
    if not isinstance(thread_id, str) or not DESKTOP_ID.fullmatch(thread_id):
        raise ClaudeDesktopError("Expected a Claude Desktop chat ID")
    if not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id):
        raise ClaudeDesktopError("Expected a mailbox message ID")
    notice = notice_text(message_id, sender)
    target = next((chat for chat in live_sessions() if chat["id"] == thread_id), None)
    if target is None or target["inbox"] is None:
        raise ClaudeDesktopError("Claude Desktop chat has no matching live inbox")

    # Installed Claude Code 2.1.325 interprets this socket frame as an
    # attributed peer notice under its normal inbound and permission controls.
    # https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket
    # Claude supplies the native peer heading. The Codex attribution is part of
    # the notice text, while the transport sender remains the bridge.
    frame = {
        "type": "user",
        "session_id": target["backend_id"],
        "from": "thread-bridge",
        "priority": "next",
        "message": {"role": "user", "content": notice},
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)  # A stalled local inbox should not block mailbox access.
        try:
            connection.connect(str(target["inbox"]))
        except OSError as error:
            raise ClaudeDesktopError("Could not open the Claude Desktop inbox") from error
        try:
            connection.sendall((json.dumps(frame) + "\n").encode("utf-8"))
        except OSError:
            return {"status": "unknown", "message_id": message_id}
    return {"status": "submitted_unconfirmed", "message_id": message_id}

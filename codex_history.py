"""Bounded local chat reads from Codex Desktop's installed SQLite projection.

The table and index contracts were checked against the installed Codex Desktop
26.1103.60933 project metadata and paginated history databases. No native
App Server session or raw rollout is opened.
"""

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


MAX_LIST = 50
MAX_TURNS = 10
MAX_MESSAGES_PER_TURN = 8
MAX_RECORD_CHARS = 256 * 1024
MAX_TEXT_CHARS = 2000
MAX_QUERY_CHARS = 160
SQL_TIMEOUT = 1.5
SQLITE_MAX_INT = 2**63 - 1

_STATE_DATABASE = "state_5.sqlite"
_HISTORY_DATABASE = "thread_history_1.sqlite"
_DESKTOP_CHAT = """
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
_CHAT_COLUMNS = """
    id, substr(name, 1, 256) AS name, substr(title, 1, 256) AS title,
    archived, updated_at_ms, recency_at_ms, history_mode
"""


class HistoryError(ValueError):
    pass


def _uuid(value):
    if not isinstance(value, str):
        raise HistoryError("Use an exact Codex chat UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise HistoryError("Use an exact Codex chat UUID") from error


def _limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise HistoryError(f"Limit must be an integer between 1 and {maximum}")
    return value


def _archive_flag(value):
    if type(value) is not bool:
        raise HistoryError("Archive selection must be true or false")
    return value


def _ordinal(value):
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise HistoryError("Invalid history cursor")
    result = int(value)
    if result > SQLITE_MAX_INT:
        raise HistoryError("Invalid history cursor")
    return result


@contextmanager
def _database(root, name):
    path = Path.home() / ".codex" if root is None else Path(root)
    path = path / name
    try:
        # Query-only is connection-local. Use mode=ro so missing databases are
        # never created and the connection can read committed WAL entries.
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.3)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            deadline = time.monotonic() + SQL_TIMEOUT
            db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
            yield db
    except sqlite3.Error as error:
        if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            raise HistoryError("The local Codex history query exceeded its time limit") from error
        raise HistoryError("The local Codex Desktop history database is unavailable or incompatible") from error


def _chat(row):
    return {
        "provider": "codex",
        "id": row["id"],
        "title": row["name"] or row["title"] or row["id"],
        "updated_at_ms": row["updated_at_ms"],
        "archived": bool(row["archived"]),
    }


def _contains_title(value, query):
    return isinstance(value, str) and query in value.casefold()


def list_threads(query=None, limit=20, cursor=None, archived=False, *, root=None):
    """List local top-level Codex Desktop chats using title or exact ID."""
    limit = _limit(limit, MAX_LIST)
    archived = _archive_flag(archived)
    if query is None:
        query = ""
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        raise HistoryError(f"Query must be text of up to {MAX_QUERY_CHARS} characters")
    query = query.strip().casefold()
    scope = hashlib.sha256(f"{int(archived)}:{query}".encode("utf-8")).hexdigest()[:12]

    filters = [_DESKTOP_CHAT, "archived = ?"]
    args = [archived]
    if query:
        try:
            exact_id = _uuid(query)
        except HistoryError:
            filters.append("(bridge_has_title(substr(name, 1, 256), ?) OR bridge_has_title(substr(title, 1, 256), ?))")
            args.extend([query, query])
        else:
            filters.append("id = ?")
            args.append(exact_id)
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 100:
            raise HistoryError("Invalid chat list cursor")
        current_scope, separator, position = cursor.partition(":")
        if not separator or current_scope != scope:
            raise HistoryError("Use a chat list cursor from the same query and archive selection")
        timestamp, _, last_id = position.partition(":")
        filters.append("(recency_at_ms, id) < (?, ?)")
        args.extend([_ordinal(timestamp), _uuid(last_id)])
    args.append(limit + 1)
    with _database(root, _STATE_DATABASE) as db:
        db.create_function("bridge_has_title", 2, _contains_title, deterministic=True)
        rows = db.execute(
            f"SELECT {_CHAT_COLUMNS} FROM threads WHERE " + " AND ".join(filters)
            + " ORDER BY recency_at_ms DESC, id DESC LIMIT ?",
            args,
        ).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more:
        last = rows[-1]
        next_cursor = f"{scope}:{last['recency_at_ms']}:{last['id']}"
    return {
        "origin": "untrusted chat metadata",
        "scope": "local Codex Desktop metadata",
        "threads": [_chat(row) for row in rows],
        "next_cursor": next_cursor,
    }


def _read_record(row):
    payload = row["item_json"]
    if payload is None:
        return None
    try:
        item = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    if not isinstance(item, dict) or item.get("type") != row["item_type"]:
        return None

    kind = row["item_type"]
    if kind == "userMessage" and isinstance(item.get("content"), list):
        text = "\n".join(
            part["text"]
            for part in item["content"]
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
        return _visible_message("user", text)
    if kind == "agentMessage":
        message = _visible_message("assistant", item.get("text"))
        if message is not None and item.get("phase") in ("commentary", "partial_answer", "final_answer"):
            message["phase"] = item["phase"]
        return message
    return _read_peer_record(item)


def _visible_message(role, text):
    if not isinstance(text, str) or not text.strip():
        return None
    return {
        "role": role,
        "text": text[:MAX_TEXT_CHARS],
        "truncated": len(text) > MAX_TEXT_CHARS,
    }


def _read_peer_record(item):
    if item.get("namespace") != "thread_bridge" or item.get("name") != "peer_message":
        return None
    output = item.get("output")
    if not isinstance(output, str):
        return None
    try:
        envelope = json.loads(output)
    except (ValueError, RecursionError):
        return None
    if not isinstance(envelope, dict) or envelope.get("schema") != "thread-bridge.peer-message/v1":
        return None
    sender = envelope.get("sender")
    message_id = envelope.get("message_id")
    message = _visible_message("peer", envelope.get("untrusted_peer_content"))
    if message is None or not isinstance(sender, dict) or not isinstance(message_id, str):
        return None
    message["message_id"] = message_id[:256]
    message["sender"] = {
        key: sender[key][:256]
        for key in ("peer", "provider", "host", "profile", "thread")
        if isinstance(sender.get(key), str)
    }
    return message


def _visible_rows(db, thread_id, turn_id):
    # The length and JSON checks apply only to this thread/turn. The installed
    # idx_thread_items_by_turn_page index supports the bounded newest-first read.
    return db.execute(
        """
        SELECT item_type, CASE WHEN length(item_json) <= ? THEN item_json END AS item_json
        FROM thread_items
        WHERE thread_id = ? AND turn_id = ? AND (
            item_type IN ('userMessage', 'agentMessage')
            OR CASE WHEN item_type = 'functionCallOutput' AND length(item_json) <= ? AND json_valid(item_json)
                THEN json_extract(item_json, '$.namespace') = 'thread_bridge'
                    AND json_extract(item_json, '$.name') = 'peer_message'
                ELSE 0 END
        )
        ORDER BY rollout_ordinal DESC
        LIMIT ?
        """,
        (MAX_RECORD_CHARS, thread_id, turn_id, MAX_RECORD_CHARS, MAX_MESSAGES_PER_TURN + 1),
    ).fetchall()


def read_thread(thread_id, *, limit=8, cursor=None, include_archived=False, root=None):
    """Read a page of user, assistant, and bridged peer messages by exact UUID."""
    thread_id = _uuid(thread_id)
    limit = _limit(limit, MAX_TURNS)
    include_archived = _archive_flag(include_archived)
    older_than = None
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 80:
            raise HistoryError("Invalid history cursor")
        scope, _, sequence = cursor.partition(":")
        if scope != thread_id:
            raise HistoryError("Use a history cursor from the same chat")
        older_than = _ordinal(sequence)

    with _database(root, _STATE_DATABASE) as db:
        row = db.execute(
            f"SELECT {_CHAT_COLUMNS} FROM threads WHERE id = ? AND {_DESKTOP_CHAT}",
            (thread_id,),
        ).fetchone()
    if row is None or row["archived"] and not include_archived:
        raise HistoryError("No matching local Codex Desktop chat in the selected archive scope")
    if row["history_mode"] != "paginated":
        raise HistoryError("This thread has no supported paginated local history")
    chat = _chat(row)

    path_filter = "AND rollout_ordinal < ?" if older_than is not None else ""
    args = [thread_id] if older_than is None else [thread_id, older_than]
    args.append(limit + 1)
    with _database(root, _HISTORY_DATABASE) as db:
        rows = db.execute(
            "SELECT turn_id, rollout_ordinal, status, started_at, completed_at "
            f"FROM thread_turns WHERE thread_id = ? {path_filter} ORDER BY rollout_ordinal DESC LIMIT ?",
            args,
        ).fetchall()
        turns = []
        for row in reversed(rows[:limit]):
            records = _visible_rows(db, thread_id, row["turn_id"])
            messages = [_read_record(item) for item in reversed(records[:MAX_MESSAGES_PER_TURN])]
            turns.append({
                "id": row["turn_id"],
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "messages": [message for message in messages if message is not None],
                "messages_truncated": len(records) > MAX_MESSAGES_PER_TURN or None in messages,
            })

    return {
        "origin": "untrusted chat history",
        "chat": chat,
        "coverage": "local paginated history projection",
        "turns": turns,
        "next_cursor": f"{thread_id}:{rows[limit - 1]['rollout_ordinal']}" if len(rows) > limit else None,
    }

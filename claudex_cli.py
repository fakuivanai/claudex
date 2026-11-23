"""Command-line tools for the local Claude and Codex desktop apps."""

import argparse
from collections.abc import Sequence
import json
import sys
from typing import assert_never

import codex_desktop
import codex_history
from desktop_control import (
    DesktopControl,
    DesktopControlRefused,
    DesktopMutation,
    DesktopSession,
    DesktopSessionRoster,
)
from models import CodexChatId, parse_codex_chat_id


class Arguments(argparse.Namespace):
    app: str
    command: str
    json: bool
    chat_id: str
    thread_id: CodexChatId
    state: str
    model: str
    effort: str
    mode: str
    query: str | None
    limit: int
    cursor: str | None
    archived: bool
    include_archived: bool


type Result = (
    DesktopSessionRoster | DesktopSession | DesktopMutation |
    codex_history.ChatPage | codex_history.ChatHistory | codex_desktop.ChatStatus
)


def page_size(value: str, maximum: int) -> int:
    try:
        size = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected an integer.") from error
    if not 1 <= size <= maximum:
        raise argparse.ArgumentTypeError(f"Expected a value from 1 to {maximum}.")
    return size


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="claudex", description=__doc__, allow_abbrev=False)
    apps = root.add_subparsers(dest="app", required=True)
    desktop = apps.add_parser("desktop", help="Control Claude Desktop", allow_abbrev=False)
    codex = apps.add_parser("codex", help="Read local Codex Desktop threads", allow_abbrev=False)
    desktop_commands = desktop.add_subparsers(dest="command", required=True)
    codex_commands = codex.add_subparsers(dest="command", required=True)

    descriptions = {
        "sessions": "List Claude Desktop chats and their Remote Control state",
        "session": "Show one Claude Desktop chat",
        "remote-control": "Set Remote Control for a chat",
        "warm": "Start the worker for a saved chat",
        "model": "Set a chat's model",
        "effort": "Set a chat's reasoning effort",
        "fast-mode": "Set fast mode for a chat",
        "permission-mode": "Set the same or a more restrictive permission mode",
        "archive": "Archive a chat",
        "unarchive": "Unarchive a chat",
        "stop": "Stop a chat's worker",
        "interrupt": "Interrupt a chat's current turn",
    }
    for name, help_text in descriptions.items():
        command = desktop_commands.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
        command.add_argument("--json", action="store_true", help="Write JSON")
        if name != "sessions":
            command.add_argument("chat_id", metavar="CHAT_ID", help="Exact Claude Desktop chat ID")
        match name:
            case "remote-control" | "fast-mode":
                command.add_argument("state", choices=("on", "off"))
            case "model":
                command.add_argument("model")
            case "effort":
                command.add_argument("effort", help="Effort value or 'default'")
            case "permission-mode":
                command.add_argument("mode")

    threads = codex_commands.add_parser("threads", help="List or search saved threads", allow_abbrev=False)
    threads.add_argument("--query", help="Title text or exact thread ID")
    threads.add_argument("--archived", action="store_true", help="List archived threads")
    threads.add_argument("--limit", type=lambda value: page_size(value, codex_history.MAX_LIST),
                         default=codex_history.DEFAULT_CHAT_PAGE)
    history = codex_commands.add_parser("history", help="Read the visible turns in a thread", allow_abbrev=False)
    history.add_argument("thread_id", type=parse_codex_chat_id, metavar="THREAD_ID")
    history.add_argument("--include-archived", action="store_true", help="Allow an archived thread")
    history.add_argument("--limit", type=lambda value: page_size(value, codex_history.MAX_TURNS),
                         default=codex_history.DEFAULT_TURN_PAGE)
    for command in (threads, history):
        command.add_argument("--cursor", help="Continue from a previous page")
        command.add_argument("--json", action="store_true", help="Write JSON")
    status = codex_commands.add_parser("status", help="Check the loaded owner for a thread", allow_abbrev=False)
    status.add_argument("thread_id", type=parse_codex_chat_id, metavar="THREAD_ID")
    status.add_argument("--json", action="store_true", help="Write JSON")
    return root


def desktop_result(args: Arguments, control: DesktopControl) -> DesktopSessionRoster | DesktopSession | DesktopMutation:
    match args.command:
        case "sessions":
            return control.list_sessions()
        case "session":
            return control.get_session(args.chat_id)
        case "remote-control":
            return control.set_remote_control(args.chat_id, args.state == "on")
        case "warm":
            return control.warm_session(args.chat_id)
        case "model":
            return control.set_model(args.chat_id, args.model)
        case "effort":
            return control.set_effort(args.chat_id, None if args.effort == "default" else args.effort)
        case "fast-mode":
            return control.set_fast_mode(args.chat_id, args.state == "on")
        case "permission-mode":
            return control.set_permission_mode(args.chat_id, args.mode)
        case "archive":
            return control.archive(args.chat_id)
        case "unarchive":
            return control.unarchive(args.chat_id)
        case "stop":
            return control.stop(args.chat_id)
        case "interrupt":
            return control.interrupt(args.chat_id)
        case _:
            raise ValueError("Unknown Claude Desktop command.")


def codex_result(args: Arguments) -> codex_history.ChatPage | codex_history.ChatHistory | codex_desktop.ChatStatus:
    match args.command:
        case "threads":
            if args.query is not None and len(args.query) > codex_history.MAX_QUERY_CHARS:
                raise ValueError(f"Use a search query of at most {codex_history.MAX_QUERY_CHARS} characters.")
            return codex_history.list_chats(
                query=args.query, limit=args.limit, cursor=args.cursor, archived=args.archived,
            )
        case "history":
            return codex_history.read_chat(
                args.thread_id, limit=args.limit, cursor=args.cursor, include_archived=args.include_archived,
            )
        case "status":
            return codex_desktop.chat_status(args.thread_id)
        case _:
            raise ValueError("Unknown Codex Desktop command.")


def printable(text: str, *, multiline: bool = False) -> str:
    return "".join(char if char.isprintable() or (multiline and char == "\n") else " " for char in text)


def print_session(session: DesktopSession, *, include_settings: bool = False) -> None:
    saved = {True: "on", False: "off", None: "unset"}[session.remote_control_saved]
    running = "yes" if session.running else "no"
    archived = "yes" if session.archived else "no"
    worker = "warm" if session.warm else "cold"
    title = printable(session.title or "Untitled")
    print(
        f"{session.chat_id}  {title}  worker {worker}, running {running}, archived {archived}, "
        f"Remote Control {session.remote_control}, saved {saved}"
    )
    if include_settings:
        fast = {True: "on", False: "off", None: "unset"}[session.fast_mode]
        print(
            f"Model {printable(session.model or 'unset')}, effort {printable(session.effort or 'default')}, "
            f"fast mode {fast}, permissions {printable(session.permission_mode or 'unset')}"
        )


def print_chat(chat: codex_history.Chat) -> None:
    title = printable(chat.title or "Untitled")
    print(f"{chat.id}  {title}" + ("  (archived)" if chat.archived else ""))


def print_cursor(cursor: str | None) -> None:
    if cursor is not None:
        print(f"Next cursor {cursor}")


def print_result(result: Result, as_json: bool) -> None:
    match result:
        case codex_history.ChatPage() | codex_history.ChatHistory() | codex_desktop.ChatStatus() if as_json:
            print(json.dumps(result.to_wire()))
        case DesktopSessionRoster() | DesktopSession() | DesktopMutation() if as_json:
            print(result.model_dump_json())
        case DesktopSessionRoster():
            if not result.sessions:
                print("No Claude Desktop chats in the loaded roster.")
            for session in result.sessions:
                print_session(session)
            if result.archived_pending:
                print("Claude Desktop has more archived chats to load.")
        case DesktopSession():
            print_session(result, include_settings=True)
        case DesktopMutation():
            if result.session is not None:
                print_session(result.session, include_settings=True)
            if result.notice:
                print(printable(result.notice))
            elif result.session is None:
                print("Claude Desktop accepted the request.")
        case codex_history.ChatPage():
            if not result.chats:
                print("No matching Codex Desktop threads.")
            for chat in result.chats:
                print_chat(chat)
            print_cursor(result.next_cursor)
        case codex_history.ChatHistory():
            print_chat(result.chat)
            for turn in result.turns:
                print()
                print(f"Turn {printable(turn.record.id)} ({turn.record.status})")
                for message in turn.messages:
                    text = printable(message.preview.text, multiline=True)
                    print(f"{message.to_wire()['role']}: {text}" + (" [truncated]" if message.preview.truncated else ""))
            print_cursor(result.next_cursor)
        case codex_desktop.ChatStatus():
            print(f"{result.chat_id}  {'loaded' if result.loaded else 'unloaded'}")
        case _:
            assert_never(result)


def main(argv: Sequence[str]) -> int:
    args = parser().parse_args(argv, namespace=Arguments())
    try:
        result: Result = desktop_result(args, DesktopControl()) if args.app == "desktop" else codex_result(args)
    except ValueError as error:
        reason = str(error)
        if args.json:
            status = "refused" if isinstance(error, DesktopControlRefused) else "error"
            print(json.dumps({"status": status, "reason": reason}))
        else:
            print(f"claudex: {printable(reason)}", file=sys.stderr)
        return 1
    print_result(result, args.json)
    return 0

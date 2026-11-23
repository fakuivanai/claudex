"""Command-line tools for the local Claude and Codex desktop apps."""

import argparse
import json
from pathlib import Path
import sys
from typing import Literal, assert_never

from claudex import codex, server
from claudex.claude.control import (
    DesktopControl,
    DesktopResult,
    EFFORTS,
    PERMISSION_MODES,
    Effort,
    PermissionMode,
    Success,
    DesktopMutation,
    DesktopChat,
    DesktopChatList,
)
from claudex.models import CodexChatId, Provider, parse_codex_chat_id


class Arguments(argparse.Namespace):
    app: str
    command: str
    json: bool
    chat_id: str
    codex_chat_id: CodexChatId
    client: Provider
    state_file: Path
    state: str
    model: str
    effort: Effort | Literal["default"]
    mode: PermissionMode
    query: str | None
    limit: int
    cursor: str | None
    archived: bool
    include_archived: bool


type Result = (
    DesktopChatList | DesktopChat | DesktopMutation |
    codex.history.ChatPage | codex.history.ChatHistory | codex.ipc.ChatStatus
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
    claude_app = apps.add_parser("claude", help="Control Claude Desktop chats through the patch", allow_abbrev=False)
    codex_app = apps.add_parser("codex", help="Read local Codex Desktop chats", allow_abbrev=False)
    mcp_app = apps.add_parser("mcp", help="Run the local MCP server over stdio", allow_abbrev=False)
    mcp_app.add_argument("--client", choices=list(Provider), type=Provider, required=True,
                         help="The app that runs this server")
    mcp_app.add_argument("--state", dest="state_file", type=Path, required=True,
                         help="SQLite mailbox file shared by both apps")
    desktop_commands = claude_app.add_subparsers(dest="command", required=True)
    codex_commands = codex_app.add_subparsers(dest="command", required=True)

    descriptions = {
        "chats": "List Claude Desktop chats and their Remote Control state",
        "chat": "Show one Claude Desktop chat",
        "remote-control": "Set Remote Control for a chat",
        "wake": "Start the worker for a saved chat",
        "model": "Set a chat's model",
        "effort": "Set a chat's reasoning effort",
        "fast-mode": "Set fast mode for a chat",
        "permission-mode": "Keep or lower a chat's permission mode",
        "archive": "Archive a chat",
        "unarchive": "Unarchive a chat",
        "stop": "Stop a chat's worker",
        "interrupt": "Interrupt a chat's current turn",
    }
    for name, help_text in descriptions.items():
        command = desktop_commands.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
        command.add_argument("--json", action="store_true", help="Print JSON")
        if name != "chats":
            command.add_argument("chat_id", metavar="CHAT_ID", help="Exact Claude Desktop chat ID")
        match name:
            case "remote-control" | "fast-mode":
                command.add_argument("state", choices=("on", "off"))
            case "model":
                command.add_argument("model")
            case "effort":
                command.add_argument("effort", choices=(*EFFORTS, "default"), help="Effort value or 'default'")
            case "permission-mode":
                command.add_argument("mode", choices=PERMISSION_MODES)

    threads = codex_commands.add_parser("chats", help="List or search saved chats", allow_abbrev=False)
    threads.add_argument("--query", help="Title text or exact chat ID")
    threads.add_argument("--archived", action="store_true", help="List archived chats")
    threads.add_argument("--limit", type=lambda value: page_size(value, codex.history.MAX_LIST),
                         default=codex.history.DEFAULT_CHAT_PAGE,
                         help=f"Chats per page, at most {codex.history.MAX_LIST}")
    history = codex_commands.add_parser("history", help="Read the visible turns in a chat", allow_abbrev=False)
    history.add_argument("codex_chat_id", type=parse_codex_chat_id, metavar="CHAT_ID")
    history.add_argument("--include-archived", action="store_true", help="Allow an archived chat")
    history.add_argument("--limit", type=lambda value: page_size(value, codex.history.MAX_TURNS),
                         default=codex.history.DEFAULT_TURN_PAGE,
                         help=f"Turns per page, at most {codex.history.MAX_TURNS}")
    for command in (threads, history):
        command.add_argument("--cursor", help="Continue from a previous page")
        command.add_argument("--json", action="store_true", help="Print JSON")
    status = codex_commands.add_parser("status", help="Check whether a Codex chat can receive peer messages", allow_abbrev=False)
    status.add_argument("codex_chat_id", type=parse_codex_chat_id, metavar="CHAT_ID")
    status.add_argument("--json", action="store_true", help="Print JSON")
    return root


def desktop_result(
    args: Arguments, control: DesktopControl,
) -> DesktopResult[DesktopChatList] | DesktopResult[DesktopChat] | DesktopResult[DesktopMutation]:
    match args.command:
        case "chats":
            return control.list_chats()
        case "chat":
            return control.get_chat(args.chat_id)
        case "remote-control":
            return control.set_remote_control(args.chat_id, args.state == "on")
        case "wake":
            return control.warm_chat(args.chat_id)
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


def codex_result(args: Arguments) -> codex.history.ChatPage | codex.history.ChatHistory | codex.ipc.ChatStatus:
    match args.command:
        case "chats":
            if args.query is not None and len(args.query) > codex.history.MAX_QUERY_CHARS:
                raise codex.history.HistoryError(f"Use a search query of at most {codex.history.MAX_QUERY_CHARS} characters.")
            return codex.history.list_chats(
                query=args.query, limit=args.limit, cursor=args.cursor, archived=args.archived,
            )
        case "history":
            return codex.history.read_chat(
                args.codex_chat_id, limit=args.limit, cursor=args.cursor, include_archived=args.include_archived,
            )
        case "status":
            return codex.ipc.chat_status(args.codex_chat_id)
        case _:
            raise ValueError("Unknown Codex Desktop command.")


def printable(text: str, *, multiline: bool = False) -> str:
    return "".join(char if char.isprintable() or (multiline and char == "\n") else " " for char in text)


def print_desktop_chat(chat: DesktopChat, *, include_settings: bool = False) -> None:
    saved = {True: "on", False: "off", None: "unset"}[chat.remote_control_saved]
    running = "yes" if chat.running else "no"
    archived = "yes" if chat.archived else "no"
    worker = "warm" if chat.warm else "cold"
    title = printable(chat.title or "Untitled")
    print(
        f"{chat.chat_id}  {title}  worker {worker}, running {running}, archived {archived}, "
        f"Remote Control {chat.remote_control}, saved {saved}"
    )
    if include_settings:
        fast = {True: "on", False: "off", None: "unset"}[chat.fast_mode]
        print(
            f"Model {printable(chat.model or 'unset')}, effort {printable(chat.effort or 'default')}, "
            f"fast mode {fast}, permissions {printable(chat.permission_mode or 'unset')}"
        )


def print_chat(chat: codex.history.Chat) -> None:
    title = printable(chat.title or "Untitled")
    print(f"{chat.id}  {title}" + ("  (archived)" if chat.archived else ""))


def print_cursor(cursor: str | None) -> None:
    if cursor is not None:
        print(f"Next cursor {cursor}")


def print_result(result: Result, as_json: bool) -> None:
    match result:
        case codex.history.ChatPage() | codex.history.ChatHistory() | codex.ipc.ChatStatus() if as_json:
            print(json.dumps(result.to_wire()))
        case DesktopChatList() | DesktopChat() | DesktopMutation() if as_json:
            print(result.model_dump_json())
        case DesktopChatList():
            if not result.chats:
                print("No Claude Desktop chats in the loaded roster.")
            for desktop_chat in result.chats:
                print_desktop_chat(desktop_chat)
            if result.archived_pending:
                print("Claude Desktop has more archived chats to load.")
        case DesktopChat():
            print_desktop_chat(result, include_settings=True)
        case DesktopMutation():
            if result.chat is not None:
                print_desktop_chat(result.chat, include_settings=True)
            if result.notice:
                print(printable(result.notice))
            elif result.chat is None:
                print("Claude Desktop accepted the request.")
        case codex.history.ChatPage():
            if not result.chats:
                print("No matching Codex Desktop chats.")
            for chat in result.chats:
                print_chat(chat)
            print_cursor(result.next_cursor)
        case codex.history.ChatHistory():
            print_chat(result.chat)
            for turn in result.turns:
                print()
                print(f"Turn {printable(turn.record.id)} ({turn.record.status})")
                for message in turn.messages:
                    text = printable(message.preview.text, multiline=True)
                    print(f"{message.to_wire()['role']}: {text}" + (" [truncated]" if message.preview.truncated else ""))
            print_cursor(result.next_cursor)
        case codex.ipc.ChatStatus():
            print(f"{result.chat_id}  {result.state.replace('_', ' ')}")
        case _:
            assert_never(result)


def print_error(status: str, reason: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"status": status, "reason": reason}))
    else:
        print(f"claudex: {printable(reason)}", file=sys.stderr)
    return 1


def run(args: Arguments) -> int:
    result: Result
    if args.app == "claude":
        response = desktop_result(args, DesktopControl())
        if not isinstance(response, Success):
            return print_error(response.status, response.reason, args.json)
        result = response.result
    else:
        try:
            result = codex_result(args)
        except (codex.history.HistoryError, codex.ipc.CodexDesktopError) as error:
            return print_error("error", str(error), args.json)
    print_result(result, args.json)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv, namespace=Arguments())
    if args.app == "mcp":
        server.serve(args.client, args.state_file)
        return 0
    return run(args)

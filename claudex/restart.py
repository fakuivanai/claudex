"""Restart a local Desktop from the user manager and keep a recovery receipt."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from claudex.claude.control import DesktopChat, DesktopControl, InvalidResponse, Success, TimedOut, UnsupportedQuit
from claudex.codex import history, ipc
from claudex.environment import SYSTEM_PATH, desktop_environment, systemd_runtime_directory
from claudex.models import Provider

# Leave time for live work to finish, Electron to quit and the replacement to connect.
WAIT_BUSY_SECONDS = 120.0
QUIT_SECONDS = 45.0
CONNECT_SECONDS = 75.0
# Claude reconnects Remote Control after its control socket opens.
RESTORE_SECONDS = 45.0
# A status call can wait for Desktop IPC. Bound the sweep and report partial coverage.
REACHABILITY_SECONDS = 20.0
POLL_SECONDS = 0.5
CALL_SECONDS = 8.0
# Includes the reachability sweep and an in-flight query at each deadline.
RECEIPT_SECONDS = 360.0
_PROCESS_ROOT = Path("/proc")
_CLAUDE_NIX_BINARY = re.compile(
    r"/nix/store/[0-9a-z]{32}-claude-desktop-[0-9A-Za-z._+-]+/lib/claude-desktop/claude-desktop",
)
_WATCHER = "org.kde.StatusNotifierWatcher"
_MENU = "com.canonical.dbusmenu"


class RestartError(RuntimeError):
    pass


@dataclass(frozen=True)
class Process:
    pid: int
    started: int
    executable: Path


@dataclass(frozen=True)
class Tray:
    service: str
    menu: str
    quit_action: int


def claude_nix_binary(folder: Path) -> Path | None:
    # The Nix launcher can make /proc/<pid>/exe inaccessible. Use its exact
    # packaged binary as a candidate. The tray still verifies the D-Bus peer PID.
    if (folder / "comm").read_text().rstrip("\n") != "claude-desktop":
        return None
    first_argument = os.fsdecode((folder / "cmdline").read_bytes().partition(b"\0")[0])
    if not _CLAUDE_NIX_BINARY.fullmatch(first_argument):
        return None
    binary = Path(first_argument)
    return binary if binary.is_file() and not binary.is_symlink() and os.access(binary, os.X_OK) else None


def process(pid: int) -> Process | None:
    folder = _PROCESS_ROOT / str(pid)
    try:
        if folder.stat().st_uid != os.getuid():
            return None
        fields = (folder / "stat").read_text().rsplit(") ", 1)[1].split()
        if fields[0] == "Z":
            return None
        try:
            executable: Path | None = (folder / "exe").readlink()
        except PermissionError:
            executable = claude_nix_binary(folder)
        return None if executable is None else Process(pid, int(fields[19]), executable)
    except (OSError, IndexError, ValueError):
        return None


def desktop_processes(app: Provider) -> tuple[Process, ...]:
    names = {"claude-desktop"} if app == Provider.CLAUDE else {".ChatGPT-wrapped", "ChatGPT"}
    result: list[Process] = []
    for folder in _PROCESS_ROOT.glob("[0-9]*"):
        try:
            item = process(int(folder.name))
            if item is not None and item.executable.name in names and b"--type=" not in (folder / "cmdline").read_bytes():
                result.append(item)
        except (OSError, ValueError):
            continue
    return tuple(result)


def selected_desktop(app: Provider) -> Process:
    matches = desktop_processes(app)
    if len(matches) != 1:
        raise RestartError(f"Expected exactly one running {app.value.title()} Desktop, found {len(matches)}.")
    return matches[0]


def app_server_children(owner: Process) -> tuple[Process, ...]:
    result: list[Process] = []
    for folder in _PROCESS_ROOT.glob("[0-9]*"):
        try:
            fields = (folder / "stat").read_text().rsplit(") ", 1)[1].split()
            if int(fields[1]) != owner.pid or b"app-server" not in (folder / "cmdline").read_bytes().split(b"\0"):
                continue
            item = process(int(folder.name))
            if item is not None and item.executable.name == "codex":
                result.append(item)
        except (OSError, IndexError, ValueError):
            continue
    return tuple(result)


def wait_for[T](probe: Callable[[], T | None], seconds: float) -> T | None:
    deadline = time.monotonic() + seconds
    while True:
        result = probe()
        remaining = deadline - time.monotonic()
        if result is not None or remaining <= 0:
            return result
        time.sleep(min(POLL_SECONDS, remaining))


def command_path(name: str) -> str:
    path = shutil.which(name, path=SYSTEM_PATH)
    if path is None:
        raise RestartError(f"Could not find {name}.")
    return path


def bus(environment: Mapping[str, str], *args: str) -> object:
    result = subprocess.run(
        [command_path("busctl"), "--user", "--json=short", *args],
        env=environment, check=True, capture_output=True, text=True, timeout=CALL_SECONDS,
    )
    if not result.stdout.strip():
        return None
    message: object = json.loads(result.stdout)
    if not isinstance(message, dict) or "data" not in message:
        raise RestartError("The Desktop tray returned an invalid D-Bus reply.")
    return message["data"]


def claude_tray(owner: Process, environment: Mapping[str, str]) -> Tray:
    # The native KDE Quit route was used in the approved Claude 5.11492.5 rollout.
    registered = bus(
        environment, "get-property", _WATCHER, "/StatusNotifierWatcher", _WATCHER, "RegisteredStatusNotifierItems",
    )
    if not isinstance(registered, list):
        raise RestartError("Claude Desktop did not advertise a tray menu.")
    actions: list[Tray] = []
    for item in registered:
        if not isinstance(item, str) or "/" not in item:
            continue
        service, _, path = item.partition("/")
        try:
            pid = bus(
                environment, "call", "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "GetConnectionUnixProcessID", "s", service,
            )
        except subprocess.CalledProcessError:
            continue
        if pid != [owner.pid] or process(owner.pid) != owner:
            continue
        menu = bus(environment, "get-property", service, "/" + path, "org.kde.StatusNotifierItem", "Menu")
        if not isinstance(menu, str) or not menu.startswith("/"):
            continue
        layout = bus(environment, "call", service, menu, _MENU, "GetLayout", "iias", "0", "1", "2", "label", "enabled")
        if not isinstance(layout, list) or len(layout) != 2 or not isinstance(layout[1], list) or len(layout[1]) != 3:
            continue
        children = layout[1][2]
        if not isinstance(children, list):
            continue
        for child in children:
            value = child.get("data") if isinstance(child, dict) else None
            if not isinstance(value, list) or len(value) != 3:
                continue
            action, properties, _ = value
            if type(action) is not int or not isinstance(properties, dict):
                continue
            label = properties.get("label")
            enabled = properties.get("enabled")
            if isinstance(label, dict) and label.get("data") == "Quit" and (
                enabled is None or isinstance(enabled, dict) and enabled.get("data") is True
            ):
                actions.append(Tray(service, menu, action))
    if len(actions) != 1:
        raise RestartError("Could not verify one enabled Quit action for the running Claude Desktop.")
    return actions[0]


def quit_desktop(app: Provider, owner: Process, environment: Mapping[str, str], *, force: bool = False) -> None:
    if app == Provider.CLAUDE:
        if process(owner.pid) != owner:
            raise RestartError("Claude Desktop changed while preparing to quit.")
        result = DesktopControl(timeout=CALL_SECONDS).quit(owner_pid=owner.pid, force=force)
        if isinstance(result, (Success, InvalidResponse, TimedOut)):
            # If the acknowledgement was lost, verify exit before relaunching.
            return
        if not isinstance(result, UnsupportedQuit):
            raise RestartError(f"Claude Desktop native quit failed: {result.reason}")
        tray = claude_tray(owner, environment)
        if process(owner.pid) != owner:
            raise RestartError("Claude Desktop changed while preparing to quit.")
        bus(environment, "call", tray.service, tray.menu, _MENU, "Event", "isvu", str(tray.quit_action), "clicked", "s", "", "0")
        return
    # A direct SIGTERM to the single browser pid shut down 26.1103.60933 gracefully in the live rollout.
    # A pidfd prevents a recycled process id from receiving the signal.
    descriptor = os.pidfd_open(owner.pid)
    try:
        if process(owner.pid) != owner:
            raise RestartError("Codex Desktop changed while preparing to quit.")
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(descriptor)


def launcher(app: Provider) -> Path:
    if app == Provider.CLAUDE:
        configured = os.environ.get("CLAUDEX_CLAUDE_LAUNCHER")
        default = Path.home() / ".local/bin/claude-desktop"
    else:
        configured = os.environ.get("CLAUDEX_CODEX_LAUNCHER")
        default = Path.home() / ".nix-profile/bin/chatgpt"
    path = Path(configured) if configured else default
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise RestartError(f"Set CLAUDEX_{app.value.upper()}_LAUNCHER to the absolute path of the managed Desktop launcher.")
    return path.resolve(strict=True)


def claude_roster() -> list[DesktopChat]:
    result = DesktopControl(timeout=CALL_SECONDS).list_chats()
    if isinstance(result, Success):
        return result.result.chats
    raise RestartError(f"Claude Desktop roster unavailable: {result.reason}")


def codex_roster() -> list[history.Chat]:
    rows: dict[str, history.Chat] = {}
    cursor = None
    while True:
        page = history.list_chats(limit=history.MAX_LIST, cursor=cursor)
        rows.update((chat.id, chat) for chat in page.chats)
        if page.next_cursor is None:
            return list(rows.values())
        cursor = page.next_cursor


def read_codex_roster() -> tuple[history.Chat, ...] | None:
    for _ in range(2):
        try:
            return tuple(codex_roster())
        except history.HistoryError:
            continue
    return None


@dataclass(frozen=True)
class Snapshot:
    running: dict[str, str]
    connected: dict[str, str]
    codex_chats: tuple[history.Chat, ...] | None = ()
    pending: bool = False

    @property
    def has_work(self) -> bool:
        return self.pending or bool(self.running)


def busy_outcome(state: Snapshot, *, timed_out: bool = False) -> dict[str, object]:
    result: dict[str, object] = {"status": "busy", "running": state.running}
    if timed_out:
        result["reason"] = "Waited two minutes for Desktop work to finish."
    if state.pending:
        result["pending_work"] = True
    return result


def snapshot(app: Provider, *, force: bool = False) -> Snapshot:
    if app == Provider.CLAUDE:
        rows = claude_roster()
        return Snapshot(
            {chat.chat_id: chat.title or chat.chat_id for chat in rows if chat.running},
            {chat.chat_id: chat.title or chat.chat_id for chat in rows if chat.remote_control == "on"},
        )
    chats = read_codex_roster()
    work = ipc.busy_chats()
    if work is None:
        if not force:
            raise RestartError("This Codex Desktop build cannot confirm which chats are running. "
                               "Install the current patch, or use --force if it is safe to interrupt any running work.")
        return Snapshot({}, {}, chats)
    running_ids, pending = work
    titles = {chat.id: chat.title for chat in chats or ()}
    return Snapshot({chat_id: titles.get(chat_id, chat_id) for chat_id in running_ids}, {}, chats, pending)


def remote_control_report(before: Snapshot) -> dict[str, object]:
    current: dict[str, str] = {}

    def restored() -> bool | None:
        nonlocal current
        current = {chat.chat_id: chat.title or chat.chat_id for chat in claude_roster() if chat.remote_control == "on"}
        return True if before.connected.keys() <= current.keys() else None

    wait_for(restored, RESTORE_SECONDS)
    return {
        "remote_control_on": len(current),
        "remote_control_restored": len(before.connected.keys() & current.keys()),
        "remote_control_chats": current,
        "remote_control_missing": {chat: before.connected[chat] for chat in before.connected.keys() - current.keys()},
    }


def codex_reachability(chats: tuple[history.Chat, ...] | None) -> dict[str, object]:
    if chats is None:
        chats = read_codex_roster()
    if chats is None:
        return {"reachability_unavailable": "The saved Codex chat list could not be read."}
    deadline = time.monotonic() + REACHABILITY_SECONDS
    checked = 0
    reachable: dict[str, str] = {}
    unreachable: dict[str, str] = {}
    for chat in chats:
        if time.monotonic() >= deadline:
            break
        checked += 1
        try:
            status = ipc.chat_status(chat.id)
        except ipc.CodexDesktopError:
            continue
        (reachable if status.reachable else unreachable)[chat.id] = chat.title
    return {
        "saved_chats": len(chats), "checked": checked, "reachable": len(reachable),
        "unknown": len(chats) - len(reachable) - len(unreachable),
        "reachable_chats": reachable, "unreachable_chats": unreachable,
    }


def socket_ready(app: Provider, prior: Process) -> Process | None:
    candidates = desktop_processes(app)
    if len(candidates) != 1 or candidates[0] == prior:
        return None
    try:
        if app == Provider.CLAUDE:
            claude_roster()
        else:
            with ipc.desktop_client(None):
                pass
    except (RestartError, ipc.CodexDesktopError):
        return None
    return candidates[0]


def require_separate_worker(owner: Process) -> None:
    current_scope = (_PROCESS_ROOT / "self/cgroup").read_text()
    if (_PROCESS_ROOT / str(owner.pid) / "cgroup").read_text() == current_scope:
        raise RestartError("The restart worker must run outside Desktop's process scope.")


def native_restart(
    app: Provider, owner: Process, path: Path, *, wait: bool, force: bool,
) -> dict[str, object]:
    environment = desktop_environment()

    def ready() -> Snapshot | None:
        if selected_desktop(app) != owner:
            raise RestartError("Desktop changed while waiting to restart.")
        state = snapshot(app)
        return state if not state.has_work else None

    before = snapshot(app, force=force)
    if before.has_work and not force:
        if not wait:
            return busy_outcome(before)
        idle = wait_for(ready, WAIT_BUSY_SECONDS)
        if idle is None:
            return busy_outcome(snapshot(app), timed_out=True)
        before = idle
    if selected_desktop(app) != owner:
        raise RestartError("Desktop changed before the restart.")
    require_separate_worker(owner)
    children = app_server_children(owner) if app == Provider.CODEX else ()
    quit_desktop(app, owner, environment, force=force)
    if wait_for(lambda: True if process(owner.pid) != owner else None, QUIT_SECONDS) is None:
        raise RestartError("Timed out waiting for Desktop to exit.")
    if wait_for(lambda: True if all(process(child.pid) != child for child in children) else None, QUIT_SECONDS) is None:
        raise RestartError("Codex Desktop closed, but its app server is still running. The replacement was not launched.")
    if desktop_processes(app):
        raise RestartError("Another Desktop instance started while the first one was quitting.")
    subprocess.run(
        [command_path("systemd-run"), "--user", "--collect", "--property=ExitType=cgroup", "--", str(path)],
        env=environment, check=True, capture_output=True, text=True, timeout=CALL_SECONDS,
    )
    replacement = wait_for(lambda: socket_ready(app, owner), CONNECT_SECONDS)
    if replacement is None:
        raise RestartError("A launch was requested, but the new Desktop socket did not become ready.")
    restored = remote_control_report(before) if app == Provider.CLAUDE else codex_reachability(before.codex_chats)
    return {"status": "restarted", "previous_pid": owner.pid, "pid": replacement.pid, **restored}


def same_desktop_context(app: Provider, owner: Process) -> bool:
    if app == Provider.CODEX and os.environ.get("CODEX_THREAD_ID"):
        return True
    if app == Provider.CLAUDE and os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "claude-desktop":
        return True
    pid = os.getpid()
    while pid > 1:
        if pid == owner.pid:
            return True
        try:
            fields = (_PROCESS_ROOT / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
            pid = int(fields[1])
        except (OSError, IndexError, ValueError):
            return False
    return False


def read_receipt(path: Path) -> dict[str, object] | None:
    try:
        payload: object = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict):
        raise RestartError("The restart receipt was invalid.")
    return payload


def emit(payload: dict[str, object], as_json: bool) -> int:
    status = payload.get("status")
    if as_json:
        print(json.dumps(payload))
    elif status == "busy":
        running = payload.get("running")
        if isinstance(running, dict) and running:
            print("Desktop still has running chats.", file=sys.stderr)
            for chat_id, title in running.items():
                print(f"  {json.dumps(title)} ({chat_id})", file=sys.stderr)
        if payload.get("pending_work"):
            print("Codex Desktop is processing work without a chat ID.", file=sys.stderr)
        if reason := payload.get("reason"):
            print(reason, file=sys.stderr)
    elif status == "scheduled":
        print(f"Desktop restart scheduled. Result receipt: {payload['receipt']}")
    elif status == "restarted":
        details = ", ".join(
            f"{key.replace('_', ' ')}: {value}" for key, value in payload.items()
            if key in {"remote_control_on", "remote_control_restored", "reachable", "checked", "unknown"}
        )
        print(f"Desktop restarted (PID {payload['pid']})" + (f". {details}" if details else "."))
        if reason := payload.get("reachability_unavailable"):
            print(reason, file=sys.stderr)
        for key, label in (("remote_control_missing", "Remote Control still off"), ("unreachable_chats", "Unreachable")):
            missing = payload.get(key)
            if isinstance(missing, dict):
                for chat_id, title in missing.items():
                    print(f"  {label}: {json.dumps(title)} ({chat_id})")
    else:
        print(payload.get("reason", "Desktop restart did not finish."), file=sys.stderr)
    return 0 if status in ("scheduled", "restarted") else 1


_RESTART_FAILURE = (OSError, RestartError, ValueError, subprocess.SubprocessError)


def restart(app: Provider, *, wait: bool = False, force: bool = False, as_json: bool = False) -> int:
    """Schedule a user-authorized Desktop restart independently of the calling app."""
    try:
        if wait and force:
            raise RestartError("Choose --wait or --force.")
        owner = selected_desktop(app)
        path = launcher(app)
        state = snapshot(app, force=force)
        if state.has_work and not (wait or force):
            return emit(busy_outcome(state), as_json)
        runtime = systemd_runtime_directory()
        if runtime is None:
            raise RestartError("Could not find the private user runtime directory for the restart receipt.")
        environment = desktop_environment()
        directory = Path(tempfile.mkdtemp(prefix=f"claudex-restart-{app.value}-", dir=runtime))
        receipt = directory / "receipt.json"
        mode = "wait" if wait else "force" if force else "idle"
        try:
            subprocess.run(
                [command_path("systemd-run"), "--user", "--collect", f"--unit=claudex-restart-{app.value}",
                 "--on-active=2s", "--timer-property=AccuracySec=1s", "--timer-property=RemainAfterElapse=no",
                 "--property=ExitType=cgroup",
                 f"--working-directory={Path(__file__).resolve().parent.parent}",
                 "--", sys.executable, "-B", "-m", "claudex.restart", app.value, str(owner.pid), str(owner.started),
                 str(owner.executable), str(path), str(receipt), mode],
                env=environment, check=True, capture_output=True, text=True, timeout=CALL_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return emit({
                "status": "unknown", "receipt": str(receipt),
                "reason": f"Scheduling was not confirmed. Check {receipt} before retrying.",
            }, as_json)
        except (OSError, subprocess.CalledProcessError):
            directory.rmdir()
            raise RestartError("Could not schedule the restart. A restart may already be pending.") from None
        if same_desktop_context(app, owner):
            return emit({"status": "scheduled", "receipt": str(receipt)}, as_json)
        result = wait_for(lambda: read_receipt(receipt), RECEIPT_SECONDS)
        return emit(result if result is not None else {
            "status": "unknown", "receipt": str(receipt), "reason": f"No final receipt yet. Check {receipt}.",
        }, as_json)
    except _RESTART_FAILURE as error:
        return emit({"status": "failed", "reason": str(error)}, as_json)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Provider, choices=list(Provider))
    parser.add_argument("pid", type=int)
    parser.add_argument("started", type=int)
    parser.add_argument("executable", type=Path)
    parser.add_argument("launcher", type=Path)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("mode", choices=("idle", "wait", "force"))
    args = parser.parse_args()
    try:
        payload = native_restart(
            args.app, Process(args.pid, args.started, args.executable), args.launcher,
            wait=args.mode == "wait", force=args.mode == "force",
        )
    except _RESTART_FAILURE as error:
        payload = {"status": "failed", "reason": str(error)}
    temporary: Path = args.receipt.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload) + "\n")
    temporary.replace(args.receipt)
    # The receipt is private. Avoid copying chat titles into the systemd journal.
    raise SystemExit(0 if payload.get("status") == "restarted" else 1)


if __name__ == "__main__":
    main()

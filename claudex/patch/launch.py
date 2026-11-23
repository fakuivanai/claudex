"""Launch Claude Desktop from the user session with an optional local patch."""

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import assert_never

from claudex.environment import desktop_environment

from . import build as archive_builder


class LaunchRefused(RuntimeError):
    """A second launcher might hand off to a different existing instance."""


# A stalled notification service should only briefly delay the stock launch.
NOTIFICATION_TIMEOUT = 2


def running_desktop(processes: Path = Path("/proc")) -> bool:
    for process in processes.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            if (process / "comm").read_text().strip() != "claude-desktop":
                continue
            arguments = (process / "cmdline").read_bytes().split(b"\0")
            if arguments[0] and not any(argument.startswith(b"--type=") for argument in arguments[1:]):
                return True
        except OSError:
            continue
    return False


def private_directory(base: Path) -> Path:
    root = base / "claudex" / "desktop"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise OSError(f"The Claudex desktop directory must be private and owned by the desktop user: {root}")
    return root


def desktop_data_directory(environment: dict[str, str], variable: str, default: Path) -> Path:
    # XDG base directory variables containing relative paths are ignored.
    configured = Path(environment.get(variable, ""))
    return private_directory(configured if configured.is_absolute() else default)


@contextmanager
def launch_lock(state: Path) -> Iterator[None]:
    # An ordinary file lock covers the window before Electron acquires its singleton.
    with (state / "launch.lock").open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LaunchRefused("A Claudex desktop launch is already in progress.") from error
        yield


def patch_digest() -> str:
    # These package files define the bytes that will be mounted in the app.
    digest = hashlib.sha256()
    directory = Path(__file__).parent
    for name in ("build.py", "archive.py", "restore.cjs", "bridge.cjs", "socket.cjs"):
        digest.update(name.encode() + b"\0")
        digest.update(hashlib.sha256((directory / name).read_bytes()).digest())
    return digest.hexdigest()


def cached_archive(source: Path, cache: Path) -> Path:
    source_key = hashlib.sha256(os.fsencode(source)).hexdigest()
    candidate = cache / f"{source_key}-{patch_digest()}.asar"
    # Only resolved Nix store paths are immutable. Rebuild and verify any
    # standalone input on each launch.
    if source.is_relative_to(archive_builder.NIX_STORE):
        try:
            mode = candidate.lstat().st_mode
            if stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o400:
                return candidate
        except FileNotFoundError:
            pass
    with tempfile.TemporaryDirectory(dir=cache) as staging:
        output = Path(staging) / "app.asar"
        archive_builder.build(source, output)
        output.chmod(0o400)
        output.replace(candidate)
    return candidate


def prune_cached_archives(cache: Path, current: Path) -> None:
    """Trim the private cache with the launch lock held and Desktop stopped."""
    for entry in cache.iterdir():
        if entry == current:
            continue
        if stat.S_ISDIR(entry.lstat().st_mode):
            shutil.rmtree(entry)
        else:
            entry.unlink()


def mount_command(bubblewrap: str, patched: Path, stock: Path, command: list[str], status_fd: int) -> list[str]:
    return [
        bubblewrap, "--json-status-fd", str(status_fd),
        "--bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc",
        "--ro-bind", str(patched), str(stock), "--", *command,
    ]


@dataclass(frozen=True)
class Started:
    status: int


@dataclass(frozen=True)
class Fallback:
    reason: str


@dataclass(frozen=True)
class Disabled:
    """The caller chose to launch Claude Desktop in stock mode."""

    reason: str = "Remote Control patch disabled"


def start_patched(
    bubblewrap: str, patched: Path, stock: Path, command: list[str], environment: dict[str, str],
) -> Started | Fallback:
    with tempfile.TemporaryFile() as status:
        fd = status.fileno()
        wrapped = mount_command(bubblewrap, patched, stock, command, fd)
        try:
            launched = subprocess.run(wrapped, env=environment, cwd=environment["HOME"], pass_fds=(fd,), check=False)
        except OSError as error:
            return Fallback(f"could not start bubblewrap ({error.strerror})")
        if launched.returncode <= 0:
            return Started(launched.returncode)
        status.seek(0)
        try:
            reports = [json.loads(line) for line in status]
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise LaunchRefused("Could not determine whether Bubblewrap started the account launcher.") from error
        # Bubblewrap reports child-pid before namespace setup. Only the
        # exit-code record confirms that setup and exec both succeeded.
        # https://github.com/containers/bubblewrap/blob/v0.11.2/bubblewrap.c
        for report in reports:
            if isinstance(report, dict):
                if type(report.get("exit-code")) is int:
                    return Started(launched.returncode)
                if type(report.get("child-pid")) is int:
                    continue
            raise LaunchRefused("Could not determine whether Bubblewrap started the account launcher.")
        return Fallback(f"patch mount setup or launcher exec failed (exit {launched.returncode})")


def notify_stock_fallback(environment: dict[str, str]) -> None:
    busctl = shutil.which("busctl", path=environment["PATH"])
    if busctl is None:
        return
    try:
        # The notification protocol uses empty action and hint arrays and the desktop's display timeout.
        # https://specifications.freedesktop.org/notification/latest/protocol.html#org.freedesktop.Notifications.Notify
        subprocess.run(
            [busctl, "--user", "--auto-start=no", "--", "call", "org.freedesktop.Notifications",
             "/org/freedesktop/Notifications", "org.freedesktop.Notifications", "Notify", "susssasa{sv}i",
             "Claudex", "0", "", "Claudex is launching stock Claude Desktop",
             "The patch could not start. Automatic Remote Control restore is off.", "0", "0", "-1"],
            env=environment, cwd=environment["HOME"], check=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=NOTIFICATION_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def stock_launch(command: list[str], environment: dict[str, str], selection: Disabled | Fallback) -> int:
    print(f"claudex: {selection.reason}. Launching stock Claude Desktop.", file=sys.stderr)
    match selection:
        case Disabled():
            pass
        case Fallback():
            notify_stock_fallback(environment)
        case _:
            assert_never(selection)
    return subprocess.run(command, env=environment, cwd=environment["HOME"], check=False).returncode


def existing_launch(command: list[str], environment: dict[str, str], reuse_running: bool) -> int:
    if not reuse_running:
        raise LaunchRefused(
            "Claude Desktop is already running. A new launch would hand off to it without changing its patch state."
        )
    arguments = command[1:]
    if not arguments:
        print("claudex: Claude Desktop is already running.", file=sys.stderr)
        return 0
    if len(arguments) != 1 or not arguments[0].startswith("claude://"):
        raise LaunchRefused("Reuse of a running Claude Desktop only accepts one claude:// link.")
    print("claudex: Handing the link to the running Claude Desktop.", file=sys.stderr)
    return subprocess.run(command, env=environment, cwd=environment["HOME"], check=False).returncode


def launch(source: Path, command: list[str], *, disabled: bool, reuse_running: bool = False) -> int:
    launcher = Path(command[0])
    if not launcher.is_absolute() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise OSError("Provide the absolute path of an executable account launcher.")
    environment = desktop_environment()
    if running_desktop():
        return existing_launch(command, environment, reuse_running)
    home = Path(environment["HOME"])
    state = desktop_data_directory(environment, "XDG_STATE_HOME", home / ".local" / "state")
    with launch_lock(state):
        if running_desktop():
            return existing_launch(command, environment, reuse_running)
        if disabled:
            return stock_launch(command, environment, Disabled())
        bubblewrap = shutil.which("bwrap", path=environment["PATH"])
        if bubblewrap is None:
            return stock_launch(command, environment, Fallback("bubblewrap unavailable in the desktop environment"))
        try:
            cache = desktop_data_directory(environment, "XDG_CACHE_HOME", home / ".cache")
            source = source.resolve(strict=True)
            patched = cached_archive(source, cache)
        except (OSError, ValueError) as error:
            return stock_launch(command, environment, Fallback(f"Remote Control patch unavailable ({error})"))
        if running_desktop():
            return existing_launch(command, environment, reuse_running)
        try:
            prune_cached_archives(cache, patched)
        except OSError as error:
            print(f"claudex: Could not remove an old cached patch ({error}).", file=sys.stderr)
        result = start_patched(bubblewrap, patched, source, command, environment)
        match result:
            case Fallback():
                return stock_launch(command, environment, result)
            case Started(status):
                return status


class Arguments(argparse.Namespace):
    source: Path
    disable: bool
    reuse_running: bool
    command: list[str]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="installed app.asar")
    parser.add_argument("--disable", action="store_true", help="run the normal stock launcher")
    parser.add_argument("--reuse-running", action="store_true", help="allow an explicit URI to use the running app")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- followed by the normal launcher and arguments")
    arguments = parser.parse_args(namespace=Arguments())
    command = arguments.command[1:] if arguments.command[:1] == ["--"] else arguments.command
    if not command:
        parser.error("pass the normal Claude Desktop launcher after --")
    try:
        status = launch(arguments.source, command, disabled=arguments.disable, reuse_running=arguments.reuse_running)
        return 128 - status if status < 0 else status
    except (OSError, LaunchRefused) as error:
        parser.exit(1, f"Claude Desktop could not launch: {error}\n")


if __name__ == "__main__":
    sys.exit(main())

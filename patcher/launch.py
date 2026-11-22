"""Launch Claude Desktop from the user session with an optional local patch."""

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from pydantic import TypeAdapter, ValidationError

from desktop_environment import desktop_environment

from . import build as archive_builder


class LaunchRefused(RuntimeError):
    """A second launcher might hand off to a different existing instance."""


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    modified: int
    changed: int

    @classmethod
    def read(cls, path: Path) -> "FileIdentity":
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"Expected a regular file at {path}")
        return cls(
            metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns,
        )


@dataclass(frozen=True)
class CacheRecord:
    source_digest: str
    artifact: FileIdentity


# The archive is reused only when the source stat identity and patch inputs match.
FILE_IDENTITY = TypeAdapter(FileIdentity)
CACHE_RECORD = TypeAdapter(CacheRecord)
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# Generated references are named by the source identity and patch digests.
CACHE_REFERENCE = re.compile(r"[0-9a-f]{64}-([0-9a-f]{64})\.json")
# A managed stock run invalidates an older Remote Control shutdown snapshot.
STOCK_LAUNCH_MARKER = "last-stock-launch"
STOCK_LAUNCH_CUTOFF = "CLAUDEX_REMOTE_CONTROL_SINCE"
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


def publish_bytes(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def patch_digest() -> str:
    # These package files define the bytes that will be mounted in the app.
    digest = hashlib.sha256()
    directory = Path(__file__).parent
    for name in ("build.py", "archive.py", "restore.cjs"):
        digest.update(name.encode() + b"\0")
        digest.update(hashlib.sha256((directory / name).read_bytes()).digest())
    return digest.hexdigest()


def cached_archive(source: Path, cache: Path) -> Path:
    identity = FileIdentity.read(source)
    patch = patch_digest()
    source_key = hashlib.sha256(os.fsencode(source) + b"\0" + FILE_IDENTITY.dump_json(identity)).hexdigest()
    reference = cache / f"{source_key}-{patch}.json"
    try:
        record = CACHE_RECORD.validate_json(reference.read_bytes(), strict=True)
        supported = SHA256_HEX.fullmatch(record.source_digest) and any(
            release.digest == record.source_digest for release in archive_builder.SUPPORTED_RELEASES
        )
        if supported:
            candidate = cache / f"{record.source_digest}-{patch}.asar"
            if FileIdentity.read(candidate) == record.artifact:
                return candidate
    except (OSError, ValidationError):
        pass

    with source.open("rb") as stream:
        release = archive_builder.identify_release(stream)
    candidate = cache / f"{release.digest}-{patch}.asar"
    with tempfile.TemporaryDirectory(dir=cache) as staging:
        output = Path(staging) / "app.asar"
        archive_builder.build(source, output)
        if FileIdentity.read(source) != identity:
            raise OSError("The installed Claude archive changed during the patch build.")
        output.chmod(0o400)
        output.replace(candidate)
    publish_bytes(reference, CACHE_RECORD.dump_json(CacheRecord(release.digest, FileIdentity.read(candidate))))
    return candidate


def prune_cached_archives(cache: Path, current: Path) -> None:
    """Remove proven stale cache entries after checking that Desktop is stopped under the launch lock."""
    if current.parent != cache:
        return
    current_identity = FileIdentity.read(current)
    for reference in cache.iterdir():
        name = CACHE_REFERENCE.fullmatch(reference.name)
        if name is None:
            continue
        try:
            info = reference.lstat()
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
                continue
            record = CACHE_RECORD.validate_json(reference.read_bytes(), strict=True)
        except (OSError, ValidationError):
            continue
        if SHA256_HEX.fullmatch(record.source_digest) is None:
            continue
        candidate = cache / f"{record.source_digest}-{name[1]}.asar"
        if candidate == current:
            if current_identity != record.artifact:
                reference.unlink(missing_ok=True)
            continue
        try:
            identity = FileIdentity.read(candidate)
        except FileNotFoundError:
            reference.unlink(missing_ok=True)
            continue
        except OSError:
            # An unreadable or nonregular artifact cannot be proven to be ours.
            continue
        if identity != record.artifact or (
            identity.device == current_identity.device and identity.inode == current_identity.inode
        ):
            continue
        candidate.unlink(missing_ok=True)
        reference.unlink(missing_ok=True)


def mount_command(bubblewrap: str, patched: Path, stock: Path, command: list[str]) -> list[str]:
    return [
        bubblewrap, "--bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc",
        "--ro-bind", str(patched), str(stock), "--", *command,
    ]


# A fresh marker records whether the account launcher was reached inside the
# mount namespace. No exit code from an already-started launcher means fallback.
START_ACCOUNT_LAUNCHER = """
from pathlib import Path
import subprocess
import sys

marker = Path(sys.argv[1])
with marker.open("x") as stream:
    stream.write("attempted")
try:
    process = subprocess.Popen(sys.argv[2:])
except OSError as error:
    marker.write_text("exec-failed")
    print(f"claudex: Could not execute the account launcher ({error.strerror}).", file=sys.stderr)
    raise SystemExit(1)
status = process.wait()
raise SystemExit(128 - status if status < 0 else status)
"""


@dataclass(frozen=True)
class Started:
    status: int


@dataclass(frozen=True)
class Fallback:
    reason: str


@dataclass(frozen=True)
class Disabled:
    """The caller chose to launch Claude Desktop in stock mode."""


def start_patched(
    bubblewrap: str, patched: Path, stock: Path, command: list[str], environment: dict[str, str], cache: Path,
) -> Started | Fallback:
    with tempfile.TemporaryDirectory(prefix="launch-", dir=cache) as directory:
        marker = Path(directory) / "account-launcher-start"
        wrapped = mount_command(
            bubblewrap, patched, stock,
            [sys.executable, "-I", "-B", "-c", START_ACCOUNT_LAUNCHER, str(marker), *command],
        )
        try:
            launched = subprocess.run(wrapped, env=environment, cwd=environment["HOME"], check=False)
        except OSError as error:
            return Fallback(f"could not start bubblewrap ({error.strerror})")
        try:
            stage = marker.read_text()
        except FileNotFoundError:
            return Fallback(f"patch mount setup failed (exit {launched.returncode})")
        if stage == "exec-failed":
            return Fallback("the account launcher could not start inside the patch mount")
        return Started(launched.returncode)


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


def stock_launch(
    command: list[str], environment: dict[str, str], state: Path, selection: Disabled | Fallback,
) -> int:
    reason = selection.reason if isinstance(selection, Fallback) else "Remote Control patch disabled"
    print(f"claudex: {reason}. Launching stock Claude Desktop.", file=sys.stderr)
    # Python and Electron both express this wall-clock cutoff in milliseconds.
    publish_bytes(state / STOCK_LAUNCH_MARKER, str(time.time_ns() // 1_000_000).encode())
    if isinstance(selection, Fallback):
        notify_stock_fallback(environment)
    return subprocess.run(command, env=environment, cwd=environment["HOME"], check=False).returncode


def stock_cutoff(state: Path) -> str:
    try:
        return str(int((state / STOCK_LAUNCH_MARKER).read_text()))
    except FileNotFoundError:
        return "0"


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
    # A stock cutoff is state, so cache eviction must not discard it.
    state = desktop_data_directory(environment, "XDG_STATE_HOME", home / ".local" / "state")
    with launch_lock(state):
        if running_desktop():
            return existing_launch(command, environment, reuse_running)
        if disabled:
            return stock_launch(command, environment, state, Disabled())
        bubblewrap = shutil.which("bwrap", path=environment["PATH"])
        if bubblewrap is None:
            return stock_launch(command, environment, state, Fallback("bubblewrap unavailable in the desktop environment"))
        try:
            cache = desktop_data_directory(environment, "XDG_CACHE_HOME", home / ".cache")
            source = source.resolve(strict=True)
            patched = cached_archive(source, cache)
            cutoff = stock_cutoff(state)
        except (OSError, ValueError) as error:
            return stock_launch(command, environment, state, Fallback(f"Remote Control patch unavailable ({error})"))
        if running_desktop():
            return existing_launch(command, environment, reuse_running)
        try:
            prune_cached_archives(cache, patched)
        except OSError as error:
            print(f"claudex: Could not remove an old cached patch ({error}).", file=sys.stderr)
        patched_environment = {**environment, STOCK_LAUNCH_CUTOFF: cutoff}
        result = start_patched(bubblewrap, patched, source, command, patched_environment, cache)
        match result:
            case Fallback():
                return stock_launch(command, environment, state, result)
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

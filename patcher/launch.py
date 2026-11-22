"""Launch Claude Desktop with a process-local reconnect patch on Linux."""

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .build import build


def mount_command(bubblewrap: str, patched: Path, stock: Path, command: list[str]) -> list[str]:
    return [
        bubblewrap, "--bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc",
        "--ro-bind", str(patched), str(stock), "--", *command,
    ]


def stock_launch(command: list[str], reason: str) -> int:
    print(f"claudex: {reason}. Launching stock Claude Desktop.", file=sys.stderr)
    return subprocess.run(command, check=False).returncode


def launch(source: Path, command: list[str], *, disabled: bool) -> int:
    if disabled:
        return stock_launch(command, "Remote Control patch disabled")
    bubblewrap = shutil.which("bwrap")
    if bubblewrap is None:
        return stock_launch(command, "bubblewrap unavailable")
    try:
        source = source.resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="claudex-desktop-") as directory:
            patched = Path(directory) / "app.asar"
            build(source, patched)
            result = subprocess.run(mount_command(bubblewrap, patched, source, command), check=False)
    except (OSError, ValueError) as error:
        return stock_launch(command, f"Remote Control patch unavailable ({error})")
    if result.returncode > 0:
        return stock_launch(command, f"patched launch exited with status {result.returncode}")
    return result.returncode


@dataclass
class Arguments(argparse.Namespace):
    source: Path = Path()
    disable: bool = False
    command: list[str] = field(default_factory=list)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="installed app.asar")
    parser.add_argument("--disable", action="store_true", help="run the normal stock launcher")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- followed by the normal launcher and arguments")
    arguments = parser.parse_args(namespace=Arguments())
    command = arguments.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("pass the normal Claude Desktop launcher after --")
    try:
        return launch(arguments.source, command, disabled=arguments.disable)
    except OSError as error:
        parser.exit(1, f"Claude Desktop could not launch: {error}\n")


if __name__ == "__main__":
    sys.exit(main())

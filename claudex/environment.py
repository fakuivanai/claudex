"""Load the graphical login's environment without inheriting the agent's shell."""

from collections.abc import Mapping
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError


# The fixed paths locate systemd before trusting the desktop session's PATH.
SYSTEM_PATH = "/run/current-system/sw/bin:/usr/local/bin:/usr/bin:/bin"
# A local user-manager property should return promptly or the launch stops.
MANAGER_TIMEOUT = 5
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_SYSTEMD_RUNTIME_ROOT = Path("/run/user")


class ManagerEnvironment(BaseModel):
    """D-Bus reports Environment as an array of unescaped strings."""

    model_config = ConfigDict(frozen=True)

    type: Literal["as"]
    data: list[str]


def parse_desktop_environment(response: str, home: Path) -> dict[str, str]:
    try:
        entries = ManagerEnvironment.model_validate_json(response, strict=True).data
    except ValidationError as error:
        raise OSError("The user desktop environment had an unexpected format.") from error
    properties: dict[str, str] = {}
    for entry in entries:
        key, separator, value = entry.partition("=")
        if not separator or not ENVIRONMENT_NAME.fullmatch(key):
            raise OSError("The user desktop environment contained a malformed entry.")
        properties[key] = value
    return clean_environment(properties, home)


def clean_environment(properties: Mapping[str, str], home: Path) -> dict[str, str]:
    private_directories = {home / ".codex"}
    codex_home = properties.get("CODEX_HOME")
    if codex_home and Path(codex_home).is_absolute():
        private_directories.add(Path(codex_home))
    private_directories = {path.resolve() for path in private_directories}

    def desktop_path(entry: str) -> bool:
        path = Path(entry)
        if not path.is_absolute():
            return False
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return False
        for candidate in (path, resolved):
            if ".codex" in candidate.parts:
                return False
            if any(candidate.is_relative_to(Path(prefix)) for prefix in ("/tmp", "/var/tmp", "/run/user")):
                if any(part.startswith("codex-") for part in candidate.parts):
                    return False
        return not any(resolved.is_relative_to(private) for private in private_directories)

    environment = {
        key: value for key, value in properties.items()
        if key != "CODEX" and not key.startswith("CODEX_")
    }
    environment["PATH"] = os.pathsep.join(
        entry for entry in properties.get("PATH", "").split(os.pathsep) if desktop_path(entry)
    )
    if not Path(environment.get("HOME", "")).is_absolute():
        environment["HOME"] = str(home)
    return environment


def systemd_runtime_directory() -> Path | None:
    """Find the private per-user runtime directory created by systemd-logind."""
    uid = os.getuid()
    directory = _SYSTEMD_RUNTIME_ROOT / str(uid)
    try:
        metadata = directory.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) != 0o700:
        return None
    return directory


def desktop_environment() -> dict[str, str]:
    """Read systemd's user Manager.Environment without invoking a shell."""
    account = pwd.getpwuid(os.getuid())
    home = Path(account.pw_dir)
    runtime = systemd_runtime_directory()
    if runtime is None:
        raise OSError("Could not find the signed-in user's private systemd runtime directory.")
    busctl = shutil.which("busctl", path=SYSTEM_PATH)
    if busctl is None:
        raise OSError("Could not locate busctl to read the user desktop environment.")
    bootstrap = {
        "HOME": str(home),
        "PATH": SYSTEM_PATH,
        "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}",
    }
    try:
        response = subprocess.run(
            [busctl, "--user", "--json=short", "get-property", "org.freedesktop.systemd1",
             "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "Environment"],
            env=bootstrap, cwd=home, check=True, capture_output=True, text=True, timeout=MANAGER_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OSError("Could not read the signed-in user's desktop environment from systemd.") from error
    environment = parse_desktop_environment(response.stdout, home)
    if not environment["PATH"] or not (environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY")):
        raise OSError("The user manager has no graphical login environment.")
    if not Path(environment.get("XDG_RUNTIME_DIR", "")).is_absolute():
        environment["XDG_RUNTIME_DIR"] = str(runtime)
    environment.setdefault("DBUS_SESSION_BUS_ADDRESS", bootstrap["DBUS_SESSION_BUS_ADDRESS"])
    return environment

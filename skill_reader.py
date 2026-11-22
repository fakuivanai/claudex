"""Read user-authored skill documents from fixed local directories."""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat


HOME_DIRECTORY = Path.home()
SKILL_ROOTS = {
    "codex": {
        "codex-user": HOME_DIRECTORY / ".codex" / "skills",
        "agents-user": HOME_DIRECTORY / ".agents" / "skills",
    },
    "claude": {"claude-user": HOME_DIRECTORY / ".claude" / "skills"},
}
MAX_SKILL_BYTES = 64 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
_UNAVAILABLE = {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EACCES, errno.EPERM}


def _roots(provider):
    try:
        return SKILL_ROOTS[provider]
    except KeyError:
        raise ValueError("Unknown skill provider") from None


def _valid_name(name):
    return (
        bool(name)
        and not name.startswith(".")
        and "/" not in name
        and "\\" not in name
        and name.isprintable()
    )


@contextmanager
def _directory(path, parent=None):
    descriptor = os.open(path, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _skill_file(directory):
    return os.stat("SKILL.md", dir_fd=directory, follow_symlinks=False)


def list_skills(provider: str) -> list[dict]:
    """List skill folder names with their provider and scope."""
    skills = []
    for scope, root in _roots(provider).items():
        try:
            with _directory(root) as root_directory:
                for name in sorted(os.listdir(root_directory)):
                    if not _valid_name(name):
                        continue
                    try:
                        with _directory(name, root_directory) as directory:
                            if stat.S_ISREG(_skill_file(directory).st_mode):
                                skills.append({"provider": provider, "scope": scope, "name": name})
                    except OSError as error:
                        if error.errno not in _UNAVAILABLE:
                            raise
        except OSError as error:
            if error.errno not in _UNAVAILABLE:
                raise
    return skills


def read_skill(provider: str, scope: str, name: str) -> dict:
    """Return the exact SKILL.md text for a listed skill."""
    root = _roots(provider).get(scope)
    if root is None:
        raise ValueError("Unknown skill scope for provider")
    if not _valid_name(name):
        raise ValueError("Invalid skill name")

    try:
        with _directory(root) as root_directory, _directory(name, root_directory) as directory:
            if not stat.S_ISREG(_skill_file(directory).st_mode):
                raise FileNotFoundError("Skill not found")
            descriptor = os.open("SKILL.md", _FILE_FLAGS, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise FileNotFoundError("Skill not found")
                if info.st_size > MAX_SKILL_BYTES:
                    raise ValueError("Skill document exceeds the size limit")
                data = stream.read(MAX_SKILL_BYTES + 1)
    except OSError as error:
        if error.errno is not None and error.errno not in _UNAVAILABLE:
            raise
        raise FileNotFoundError("Skill not found") from None

    if len(data) > MAX_SKILL_BYTES:
        raise ValueError("Skill document exceeds the size limit")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Skill document is not valid UTF-8") from None
    return {"provider": provider, "scope": scope, "name": name, "content": content}

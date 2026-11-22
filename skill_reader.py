"""Read literal SKILL.md files from user skill directories."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
import errno
import os
from pathlib import Path
import stat

from models import Provider


class SkillScope(StrEnum):
    CODEX_USER = "codex-user"
    AGENTS_USER = "agents-user"
    CLAUDE_USER = "claude-user"


@dataclass(frozen=True)
class SkillReference:
    provider: Provider
    scope: SkillScope
    name: str


@dataclass(frozen=True)
class SkillDocument:
    reference: SkillReference
    content: str


SKILL_SCOPES = {
    Provider.CODEX: (SkillScope.CODEX_USER, SkillScope.AGENTS_USER),
    Provider.CLAUDE: (SkillScope.CLAUDE_USER,),
}
# Skills are returned inline as literal text, so cap the file before reading.
MAX_SKILL_BYTES = 64 * 1024
# Follow installed skill directory links. The document itself must be a regular file.
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
_UNAVAILABLE = {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EACCES, errno.EPERM}


def _root(scope: SkillScope) -> Path:
    match scope:
        case SkillScope.CODEX_USER:
            return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "skills"
        case SkillScope.AGENTS_USER:
            return Path.home() / ".agents" / "skills"
        case SkillScope.CLAUDE_USER:
            return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "skills"


def _valid_name(name: str) -> bool:
    return bool(name and not name.startswith(".") and "/" not in name and "\\" not in name and name.isprintable())


@contextmanager
def _directory(path: str | Path, parent: int | None = None) -> Iterator[int]:
    descriptor = os.open(path, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _regular_skill(directory: int) -> bool:
    return stat.S_ISREG(os.stat("SKILL.md", dir_fd=directory, follow_symlinks=False).st_mode)


def list_skills(provider: Provider) -> list[SkillReference]:
    skills: list[SkillReference] = []
    for scope in SKILL_SCOPES[provider]:
        try:
            with _directory(_root(scope)) as root:
                for name in sorted(os.listdir(root)):
                    if not _valid_name(name):
                        continue
                    try:
                        with _directory(name, root) as directory:
                            if _regular_skill(directory):
                                skills.append(SkillReference(provider, scope, name))
                    except OSError as error:
                        if error.errno not in _UNAVAILABLE:
                            raise
        except OSError as error:
            if error.errno not in _UNAVAILABLE:
                raise
    return skills


def read_skill(provider: Provider, scope: str, name: str) -> SkillDocument:
    try:
        skill_scope = SkillScope(scope)
    except ValueError:
        raise ValueError("Unknown skill scope for provider") from None
    if skill_scope not in SKILL_SCOPES[provider]:
        raise ValueError("Unknown skill scope for provider")
    if not _valid_name(name):
        raise ValueError("Invalid skill name")

    try:
        with _directory(_root(skill_scope)) as root, _directory(name, root) as directory:
            if not _regular_skill(directory):
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
    return SkillDocument(SkillReference(provider, skill_scope, name), content)

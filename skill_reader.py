"""Read literal SKILL.md files from user skill directories."""

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


def list_skills(provider: Provider) -> list[SkillReference]:
    skills: list[SkillReference] = []
    for scope in SKILL_SCOPES[provider]:
        try:
            directories = sorted(_root(scope).iterdir())
        except OSError as error:
            if error.errno not in _UNAVAILABLE:
                raise
            continue
        for directory in directories:
            if not _valid_name(directory.name):
                continue
            try:
                if stat.S_ISREG((directory / "SKILL.md").lstat().st_mode):
                    skills.append(SkillReference(provider, scope, directory.name))
            except OSError as error:
                if error.errno not in _UNAVAILABLE:
                    raise
    return skills


def read_skill(provider: Provider, scope: SkillScope, name: str) -> SkillDocument:
    if scope not in SKILL_SCOPES[provider]:
        raise ValueError("Unknown skill scope for provider")
    if not _valid_name(name):
        raise ValueError("Invalid skill name")

    path = _root(scope) / name / "SKILL.md"
    try:
        # Directory links select installed skills. Refuse a linked document,
        # and avoid waiting on a FIFO if the path was replaced after listing.
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise FileNotFoundError("Skill not found")
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
    return SkillDocument(SkillReference(provider, scope, name), content)

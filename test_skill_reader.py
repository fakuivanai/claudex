import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from skill_reader import MAX_SKILL_BYTES, SkillProvider, SkillReference, SkillScope, list_skills, read_skill


class SkillReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="claudex-skills-")))
        self.enterContext(patch.object(Path, "home", return_value=self.root))
        self.enterContext(patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
        }))

    def write_skill(self, root: str, name: str, content: bytes) -> Path:
        directory = self.root / root / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_bytes(content)
        return directory

    def test_codex_scopes_list_duplicate_names_separately(self) -> None:
        self.write_skill("codex", "same", b"One")
        self.write_skill(".agents", "same", b"Two")
        self.write_skill("claude", "same", b"Three")
        self.assertEqual(list_skills("codex"), [
            SkillReference(SkillProvider.CODEX, SkillScope.CODEX_USER, "same"),
            SkillReference(SkillProvider.CODEX, SkillScope.AGENTS_USER, "same"),
        ])

    def test_scope_selects_the_exact_file(self) -> None:
        self.write_skill("codex", "same", b"One")
        self.write_skill(".agents", "same", b"Two")
        self.assertEqual(read_skill("codex", "agents-user", "same").content, "Two")

    def test_scope_cannot_select_another_providers_files(self) -> None:
        with self.assertRaises(ValueError):
            read_skill("codex", "claude-user", "same")

    def test_path_traversals_are_rejected(self) -> None:
        for name in ("", "..", "../outside", "sub/skill", "/etc/passwd", "x\0y"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_skill("codex", "codex-user", name)

    def test_skips_symlinks_and_nonregular_files(self) -> None:
        outside = self.write_skill("outside", "skill", b"A private file")
        skills = self.root / "claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "linked-directory").symlink_to(outside, target_is_directory=True)
        (skills / "linked-file").mkdir()
        (skills / "linked-file" / "SKILL.md").symlink_to(outside / "SKILL.md")
        (skills / "pipe").mkdir()
        os.mkfifo(skills / "pipe" / "SKILL.md")
        self.assertEqual(list_skills("claude"), [])

    def test_cannot_read_symlinked_skill_document(self) -> None:
        outside = self.write_skill("outside", "skill", b"A private file")
        linked = self.root / "claude" / "skills" / "linked"
        linked.mkdir(parents=True)
        (linked / "SKILL.md").symlink_to(outside / "SKILL.md")
        with self.assertRaises(FileNotFoundError):
            read_skill("claude", "claude-user", "linked")

    def test_limits_size(self) -> None:
        self.write_skill("claude", "large", b"x" * (MAX_SKILL_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "size limit"):
            read_skill("claude", "claude-user", "large")

    def test_rejects_invalid_utf8(self) -> None:
        self.write_skill("claude", "invalid", b"\xff")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            read_skill("claude", "claude-user", "invalid")

    def test_returns_literal_markdown(self) -> None:
        content = "---\r\nname: fixture\r\n---\r\n!\x60printf hello\x60\r\n"
        self.write_skill("claude", "fixture", content.encode("utf-8"))
        self.assertEqual(read_skill("claude", "claude-user", "fixture").content, content)

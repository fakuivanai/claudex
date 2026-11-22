import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import skill_reader


class SkillReaderTest(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory(prefix="thread-bridge-skills-"))
        self.directory = Path(temporary)
        self.roots = {
            "codex": {
                "codex-user": self.directory / "codex",
                "agents-user": self.directory / "agents",
            },
            "claude": {"claude-user": self.directory / "claude"},
        }
        self.enterContext(patch.object(skill_reader, "SKILL_ROOTS", self.roots))

    def write_skill(self, provider, scope, name, data):
        directory = self.roots[provider][scope] / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_bytes(data)
        return directory

    def test_scoped_duplicates_read_separately(self):
        self.write_skill("codex", "codex-user", "same", b"First")
        self.write_skill("codex", "agents-user", "same", b"Second")
        claude = self.write_skill("claude", "claude-user", "same", b"Third")
        (claude / "other.md").write_text("Unlisted support file", encoding="utf-8")
        self.assertEqual(
            skill_reader.list_skills("codex"),
            [
                {"provider": "codex", "scope": "codex-user", "name": "same"},
                {"provider": "codex", "scope": "agents-user", "name": "same"},
            ],
        )
        self.assertEqual(skill_reader.read_skill("codex", "codex-user", "same")["content"], "First")
        self.assertEqual(skill_reader.read_skill("codex", "agents-user", "same")["content"], "Second")
        self.assertEqual(skill_reader.read_skill("claude", "claude-user", "same")["content"], "Third")
        with self.assertRaises(ValueError):
            skill_reader.read_skill("codex", "claude-user", "same")

    def test_rejects_path_arguments_and_unknown_provider(self):
        for name in ("", "..", "../outside", "sub/skill", "/etc/passwd", "x\0y"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                skill_reader.read_skill("codex", "codex-user", name)
        with self.assertRaises(ValueError):
            skill_reader.list_skills("other")
        self.assertEqual(skill_reader.list_skills("codex"), [])

    def test_skips_symlinks_and_nonregular_files(self):
        outside = self.directory / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("Outside", encoding="utf-8")
        root = self.roots["claude"]["claude-user"]
        root.mkdir()
        (root / "linked-folder").symlink_to(outside, target_is_directory=True)
        (root / "linked-file").mkdir()
        (root / "linked-file" / "SKILL.md").symlink_to(outside / "SKILL.md")
        (root / "pipe").mkdir()
        os.mkfifo(root / "pipe" / "SKILL.md")
        (root / "folder").mkdir()
        (root / "folder" / "SKILL.md").mkdir()
        self.assertEqual(skill_reader.list_skills("claude"), [])
        for name in ("linked-folder", "linked-file", "pipe", "folder", "missing"):
            with self.subTest(name=name), self.assertRaises(FileNotFoundError):
                skill_reader.read_skill("claude", "claude-user", name)

    def test_limits_size_and_requires_utf8(self):
        self.write_skill("claude", "claude-user", "large", b"x" * (skill_reader.MAX_SKILL_BYTES + 1))
        self.write_skill("claude", "claude-user", "invalid", b"\xff")
        with self.assertRaisesRegex(ValueError, "size limit"):
            skill_reader.read_skill("claude", "claude-user", "large")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            skill_reader.read_skill("claude", "claude-user", "invalid")

    def test_returns_literal_markdown(self):
        content = "---\r\nname: fixture\r\n---\r\n!\x60printf hello\x60\r\n"
        self.write_skill("claude", "claude-user", "fixture", content.encode("utf-8"))
        self.assertEqual(
            skill_reader.read_skill("claude", "claude-user", "fixture")["content"],
            content,
        )


if __name__ == "__main__":
    unittest.main()

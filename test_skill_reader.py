import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from models import Provider
from skill_reader import MAX_SKILL_BYTES, SkillReference, SkillScope, list_skills, read_skill


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
        self.assertEqual(list_skills(Provider.CODEX), [
            SkillReference(Provider.CODEX, SkillScope.CODEX_USER, "same"),
            SkillReference(Provider.CODEX, SkillScope.AGENTS_USER, "same"),
        ])

    def test_scope_selects_the_exact_file(self) -> None:
        self.write_skill("codex", "same", b"One")
        self.write_skill(".agents", "same", b"Two")
        self.assertEqual(read_skill(Provider.CODEX, "agents-user", "same").content, "Two")

    def test_scope_cannot_select_another_providers_files(self) -> None:
        with self.assertRaises(ValueError):
            read_skill(Provider.CODEX, "claude-user", "same")

    def test_path_traversals_are_rejected(self) -> None:
        for name in ("", "..", "../outside", "sub/skill", "/etc/passwd", "x\0y"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_skill(Provider.CODEX, "codex-user", name)

    def test_skips_linked_documents_and_nonregular_files(self) -> None:
        outside = self.write_skill("outside", "skill", b"A private file")
        skills = self.root / "claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "linked-file").mkdir()
        (skills / "linked-file" / "SKILL.md").symlink_to(outside / "SKILL.md")
        (skills / "pipe").mkdir()
        os.mkfifo(skills / "pipe" / "SKILL.md")
        self.assertEqual(list_skills(Provider.CLAUDE), [])

    def test_cannot_read_symlinked_skill_document(self) -> None:
        outside = self.write_skill("outside", "skill", b"A private file")
        linked = self.root / "claude" / "skills" / "linked"
        linked.mkdir(parents=True)
        (linked / "SKILL.md").symlink_to(outside / "SKILL.md")
        with self.assertRaises(FileNotFoundError):
            read_skill(Provider.CLAUDE, "claude-user", "linked")

    def test_read_follows_user_installed_directory_link(self) -> None:
        outside = self.write_skill("outside", "skill", b"User-owned skill")
        linked = self.root / "claude" / "skills" / "linked"
        linked.parent.mkdir(parents=True)
        linked.symlink_to(outside, target_is_directory=True)
        self.assertEqual(read_skill(Provider.CLAUDE, "claude-user", "linked").content, "User-owned skill")

    def test_reads_a_link_to_a_read_only_store_directory(self) -> None:
        self.assertEqual(self.read_linked_store_skill(mode=0o555), "Managed skill")

    def test_refuses_an_unowned_writable_store_directory(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.read_linked_store_skill(mode=0o755)

    def read_linked_store_skill(self, *, mode: int) -> str:
        managed = self.write_skill("store", "skill", b"Managed skill")
        managed.chmod(mode)
        self.addCleanup(managed.chmod, 0o755)
        linked = self.root / "claude" / "skills" / "linked"
        linked.parent.mkdir(parents=True)
        linked.symlink_to(managed, target_is_directory=True)
        actual_fstat = os.fstat
        managed_identity = (managed.stat().st_dev, managed.stat().st_ino)

        def report_managed_owner(descriptor: int) -> os.stat_result:
            entry = actual_fstat(descriptor)
            if (entry.st_dev, entry.st_ino) == managed_identity and stat.S_ISDIR(entry.st_mode):
                values = list(entry)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return entry

        with (
            patch("skill_reader._NIX_STORE", self.root / "store"),
            patch("skill_reader.os.fstat", side_effect=report_managed_owner),
        ):
            return read_skill(Provider.CLAUDE, "claude-user", "linked").content

    def test_limits_size(self) -> None:
        self.write_skill("claude", "large", b"x" * (MAX_SKILL_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "size limit"):
            read_skill(Provider.CLAUDE, "claude-user", "large")

    def test_rejects_invalid_utf8(self) -> None:
        self.write_skill("claude", "invalid", b"\xff")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            read_skill(Provider.CLAUDE, "claude-user", "invalid")

    def test_returns_literal_markdown(self) -> None:
        content = "---\r\nname: fixture\r\n---\r\n!\x60printf hello\x60\r\n"
        self.write_skill("claude", "fixture", content.encode("utf-8"))
        self.assertEqual(read_skill(Provider.CLAUDE, "claude-user", "fixture").content, content)

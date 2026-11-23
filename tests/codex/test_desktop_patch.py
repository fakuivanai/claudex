import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from claudex.codex import desktop_patch
from claudex.patch.archive import Directory, Packed, encode_header, integrity_of, packed_entry, read_header


class DesktopPatchTests(unittest.TestCase):
    def test_build_registers_exactly_one_handler_and_keeps_other_resources(self) -> None:
        manager = desktop_patch.OWNER_BINDING
        other = b"unchanged resource"
        chunk = desktop_patch.HOST_MANAGER[-1]
        entries = Directory((
            (chunk, Packed(0, len(manager), integrity_of(manager, 64), None)),
            ("unrelated", Packed(len(manager), len(other), integrity_of(other, 64), None)),
        ))
        archive = Directory(((".vite", Directory((("build", entries),))),))
        stock = encode_header(archive) + manager + other
        with TemporaryDirectory() as directory:
            source = Path(directory) / "stock.asar"
            output = Path(directory) / "patched.asar"
            source.write_bytes(stock)
            with patch("claudex.codex.desktop_patch.SOURCE_DIGEST", hashlib.sha256(stock).hexdigest()):
                desktop_patch.build(source, output)
            with output.open("rb") as file:
                header = read_header(file)
                for name, content in (
                    (chunk, desktop_patch.PREPARE_BINDING),
                    ("unrelated", other),
                    (desktop_patch.PREPARE_MODULE, Path(desktop_patch.__file__).with_name("prepare.cjs").read_bytes()),
                ):
                    entry = packed_entry(header.root, (".vite", "build", name))
                    file.seek(header.body_offset + entry.offset)
                    self.assertEqual(file.read(entry.size), content)
                    self.assertEqual(entry.integrity, integrity_of(content, 64))

    def test_rejects_missing_and_duplicate_vendor_bindings(self) -> None:
        for contents in (b"", desktop_patch.OWNER_BINDING * 2):
            with self.assertRaisesRegex(ValueError, "binding did not match"):
                desktop_patch.patch_host_manager(contents)

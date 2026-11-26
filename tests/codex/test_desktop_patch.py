import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from claudex.codex import desktop_patch
from claudex.patch.archive import Directory, Packed, encode_header, integrity_of, packed_entry, read_header


class DesktopPatchTests(unittest.TestCase):
    def test_build_patches_both_turn_owners_and_keeps_other_resources(self) -> None:
        manager = desktop_patch.OWNER_BINDING + desktop_patch.HOST_TURN_ACK
        renderer = desktop_patch.RENDERER_TURN_ACK
        other = b"unchanged resource"
        manager_path = desktop_patch.HOST_MANAGER
        renderer_path = desktop_patch.RENDERER_MANAGER
        entries = Directory((
            (manager_path[-1], Packed(0, len(manager), integrity_of(manager, 64), None)),
            ("unrelated", Packed(len(manager), len(other), integrity_of(other, 64), None)),
        ))
        renderer_entries = Directory((
            (renderer_path[-1], Packed(len(manager) + len(other), len(renderer), integrity_of(renderer, 64), None)),
        ))
        archive = Directory((
            (".vite", Directory((("build", entries),))),
            ("webview", Directory((("assets", Directory((("async", renderer_entries),))),))),
        ))
        stock = encode_header(archive) + manager + other + renderer
        with TemporaryDirectory() as directory:
            source = Path(directory) / "stock.asar"
            output = Path(directory) / "patched.asar"
            source.write_bytes(stock)
            with patch("claudex.codex.desktop_patch.SOURCE_DIGEST", hashlib.sha256(stock).hexdigest()):
                desktop_patch.build(source, output)
            with output.open("rb") as file:
                header = read_header(file)
                for resource, content in (
                    (manager_path, desktop_patch.PREPARE_BINDING + desktop_patch.HOST_TURN_ACK_PATCH),
                    (renderer_path, desktop_patch.RENDERER_TURN_ACK_PATCH),
                    ((*manager_path[:-1], "unrelated"), other),
                    ((*manager_path[:-1], desktop_patch.PREPARE_MODULE),
                     Path(desktop_patch.__file__).with_name("prepare.cjs").read_bytes()),
                ):
                    entry = packed_entry(header.root, resource)
                    file.seek(header.body_offset + entry.offset)
                    self.assertEqual(file.read(entry.size), content)
                    self.assertEqual(entry.integrity, integrity_of(content, 64))

    def test_rejects_missing_and_duplicate_vendor_bindings(self) -> None:
        for contents in (b"", desktop_patch.OWNER_BINDING * 2):
            with self.assertRaisesRegex(ValueError, "binding did not match"):
                desktop_patch.patch_host_manager(contents)

    def test_rejects_missing_and_duplicate_turn_acknowledgements(self) -> None:
        for copies in (0, 2):
            with (
                self.subTest(owner="host", copies=copies),
                self.assertRaisesRegex(ValueError, "acknowledgement"),
            ):
                desktop_patch.patch_host_manager(desktop_patch.OWNER_BINDING + desktop_patch.HOST_TURN_ACK * copies)
            with (
                self.subTest(owner="renderer", copies=copies),
                self.assertRaisesRegex(ValueError, "acknowledgement"),
            ):
                desktop_patch.patch_renderer_manager(desktop_patch.RENDERER_TURN_ACK * copies)

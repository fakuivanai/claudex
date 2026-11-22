from contextlib import redirect_stderr
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from patcher.archive import Directory, Packed, Unpacked, encode_header, integrity_of, packed_entry, read_header
from patcher.build import Anchor, DesktopRelease, MODULE_NAME, STARTUP_HOOK, build, patch_chunk
from patcher.launch import launch


class PatcherTests(unittest.TestCase):
    def test_startup_hook_uses_vendor_scope_after_the_stock_pass(self) -> None:
        harness = """
        const assert = require("node:assert/strict");
        const events = [];
        const vendor = { I0: () => "signed-in account" };
        const injectedRequire = () => ({
          restoreAtLaunch: (_manager, epoch, platform) => {
            events.push([platform.account(), epoch]);
          },
        });
        const owner = new Function(
          "t", "require", "return ({" + process.argv[1] + "});"
        )(vendor, injectedRequire);
        owner.launchPassChain = Promise.resolve();
        owner.launchPass = { runLaunchPass: async () => events.push("stock") };
        owner.resumeQuitInterruptedSessionsAtLaunch(3).then(() => {
          assert.deepEqual(events, ["stock", ["signed-in account", 3]]);
        }).catch(error => {
          console.error(error);
          process.exitCode = 1;
        });
        """
        outcome = subprocess.run(
            [os.environ.get("CLAUDEX_NODE", "node"), "-e", harness, STARTUP_HOOK.decode()],
            capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(outcome.returncode, 0, outcome.stderr)

    def test_build_preserves_alias_offsets_and_unpacked_entries(self) -> None:
        prefix, old, tail = b"prefix", b"stock hook", b"identical resource"
        replacement = b"patched hook"
        target_path = ("session.js",)
        integrity = integrity_of(tail, 16)
        directory = Directory((
            ("prefix", Packed(0, len(prefix), integrity_of(prefix, 16), None)),
            ("session.js", Packed(len(prefix), len(old), integrity_of(old, 16), None)),
            ("resource", Packed(len(prefix) + len(old), len(tail), integrity, None)),
            ("resource-alias", Packed(len(prefix) + len(old), len(tail), integrity, None)),
            ("resource-unpacked", Unpacked(len(tail), integrity)),
        ))
        stock = encode_header(directory) + prefix + old + tail
        fixture = DesktopRelease("fixture", hashlib.sha256(stock).hexdigest(), target_path, (
            Anchor("fixture", old, replacement),
        ))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "stock.asar"
            output = Path(temporary) / "patched.asar"
            source.write_bytes(stock)
            with patch("patcher.build.SUPPORTED_RELEASES", (fixture,)):
                build(source, output)
            with output.open("rb") as file:
                header = read_header(file)
                resource = packed_entry(header.root, ("resource",))
                alias = packed_entry(header.root, ("resource-alias",))
                file.seek(header.body_offset + alias.offset)
                self.assertEqual((alias, file.read(resource.size)), (resource, tail))
                self.assertIn(("resource-unpacked", Unpacked(len(tail), integrity)), header.root.children)
                self.assertGreater(packed_entry(header.root, (MODULE_NAME,)).size, 0)

    def test_build_leaves_an_existing_output_intact(self) -> None:
        old = b"stock"
        source_bytes = encode_header(Directory((("session.js", Packed(0, len(old), integrity_of(old, 16), None)),))) + old
        fixture = DesktopRelease("fixture", hashlib.sha256(source_bytes).hexdigest(), ("session.js",), (
            Anchor("fixture", old, b"patched"),
        ))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "stock.asar"
            output = Path(temporary) / "patched.asar"
            source.write_bytes(source_bytes)
            output.write_bytes(b"existing")
            with patch("patcher.build.SUPPORTED_RELEASES", (fixture,)):
                with self.assertRaises(FileExistsError):
                    build(source, output)
            self.assertEqual(output.read_bytes(), b"existing")

    def test_refuses_an_unknown_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "unknown.asar"
            output = Path(temporary) / "patched.asar"
            source.write_bytes(b"unsupported")
            with self.assertRaisesRegex(ValueError, "unsupported Claude Desktop ASAR SHA-256"):
                build(source, output)
            self.assertFalse(output.exists())

    def test_refuses_an_ambiguous_hook(self) -> None:
        fixture = DesktopRelease("fixture", "", ("session.js",), (Anchor("fixture", b"stock", b"new"),))
        with self.assertRaisesRegex(ValueError, "fixture reconnect patch anchor changed"):
            patch_chunk(b"stock stock", fixture)

    def test_disabled_launch_runs_stock_without_building(self) -> None:
        with patch("patcher.launch.build") as build_archive, patch("patcher.launch.subprocess.run") as start:
            start.return_value.returncode = 0
            with redirect_stderr(io.StringIO()):
                launch(Path("stock.asar"), ["claude"], disabled=True)
            self.assertEqual((build_archive.call_count, start.call_args.args), (0, (["claude"],)))

    def test_build_failure_launches_stock(self) -> None:
        status = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "unknown.asar"
            source.write_bytes(b"unsupported")
            with patch("patcher.launch.shutil.which", return_value="bwrap"), patch("patcher.launch.subprocess.run") as start:
                start.return_value.returncode = 0
                with redirect_stderr(status):
                    launch(source, ["claude"], disabled=False)
                start.assert_called_once_with(["claude"], check=False)


if __name__ == "__main__":
    unittest.main()

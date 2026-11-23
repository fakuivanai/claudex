import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from claudex.patch.archive import Directory, Packed, Unpacked, encode_header, integrity_of, packed_entry, read_header
from claudex.patch.build import (
    Anchor, BRIDGE_MODULE, DEFERRED_ROSTER_HOOK, DesktopRelease, MODULE_NAME, SOCKET_MODULE,
    STARTUP_HOOK, WINDOW_API, build, patch_chunk,
)


class PatcherTests(unittest.TestCase):
    def run_node(self, harness: str, hook: bytes) -> None:
        outcome = subprocess.run(
            [os.environ.get("CLAUDEX_NODE", "node"), "-e", harness, hook.decode()],
            capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(outcome.returncode, 0, outcome.stderr)

    def test_startup_hook_uses_vendor_scope_after_the_stock_pass(self) -> None:
        harness = """
        const assert = require("node:assert/strict");
        const events = [];
        const featureFlags = new Map([["976614668", true]]);
        const vendor = {
          I0: () => "signed-in account",
          ldt: key => featureFlags.get(key) ?? false,
          DSt: () => ({ overlayApplied: async () => {} }),
          ATt: async overlay => overlay,
        };
        const policy = { l: () => undefined, o: async () => "allowed" };
        const injectedRequire = () => ({
          restoreAtLaunch: async (_manager, epoch, platform) => {
            events.push([platform.account(), epoch]);
            assert.equal(await platform.canRestore(), true);
            featureFlags.set("976614668", false);
            assert.equal(await platform.canRestore(), false);
            featureFlags.set("976614668", true);
            featureFlags.set("4267904482", true);
            assert.equal(await platform.canRestore(), false);
          },
        });
        const owner = new Function(
          "t", "se", "require", "return ({" + process.argv[1] + "});"
        )(vendor, policy, injectedRequire);
        owner.launchPassChain = Promise.resolve();
        owner.launchPass = { runLaunchPass: async () => events.push("stock") };
        owner.resumeQuitInterruptedSessionsAtLaunch(3).then(() => {
          assert.deepEqual(events, ["stock", ["signed-in account", 3]]);
        }).catch(error => {
          console.error(error);
          process.exitCode = 1;
        });
        """
        self.run_node(harness, STARTUP_HOOK)

    def test_deferred_roster_hook_considers_saved_on_and_native_markers(self) -> None:
        harness = """
        const assert = require("node:assert/strict");
        const seen = [];
        const check = new Function("w", "e", process.argv[1]).bind({
          resumeQuitInterruptedSessionsAtLaunch: epoch => seen.push(epoch),
        });
        check([{ remoteControlUserEnabled: true }], 1);
        check([{ remoteControlUserEnabled: false, remoteControlUserRequested: true }], 2);
        check([{ remoteControlUserRequested: true }], 3);
        check([{ interruptedByQuitAt: 123 }], 4);
        check([{ remoteControlLiveAtUpdateQuit: {} }], 5);
        assert.deepEqual(seen, [1, 4, 5]);
        """
        self.run_node(harness, DEFERRED_ROSTER_HOOK)

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
            with patch("claudex.patch.build.SUPPORTED_RELEASES", (fixture,)):
                build(source, output)
            with output.open("rb") as file:
                header = read_header(file)
                resource = packed_entry(header.root, ("resource",))
                alias = packed_entry(header.root, ("resource-alias",))
                file.seek(header.body_offset + alias.offset)
                self.assertEqual((alias, file.read(resource.size)), (resource, tail))
                self.assertIn(("resource-unpacked", Unpacked(len(tail), integrity)), header.root.children)
                self.assertGreater(packed_entry(header.root, (MODULE_NAME,)).size, 0)

    def test_build_patches_both_chunks_preserving_the_bytes_between_them(self) -> None:
        chunks = (
            ("window.js", WINDOW_API.stock),
            ("untouched", b"resource between the patched chunks"),
            ("session.js", b"session hook"),
        )
        body = b""
        files: list[tuple[str, Packed]] = []
        for name, content in chunks:
            files.append((name, Packed(len(body), len(content), integrity_of(content, 16), None)))
            body += content
        stock = encode_header(Directory(tuple(files))) + body
        fixture = DesktopRelease(
            "fixture", hashlib.sha256(stock).hexdigest(), ("session.js",),
            (Anchor("manager", b"session hook", b"manager replacement"),), ("window.js",),
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "stock.asar"
            output = Path(temporary) / "patched.asar"
            source.write_bytes(stock)
            with patch("claudex.patch.build.SUPPORTED_RELEASES", (fixture,)):
                build(source, output)
            with output.open("rb") as file:
                header = read_header(file)
                for name, content in (
                    ("window.js", WINDOW_API.patched), ("session.js", b"manager replacement"), chunks[1],
                ):
                    entry = packed_entry(header.root, (name,))
                    file.seek(header.body_offset + entry.offset)
                    self.assertEqual(file.read(entry.size), content)
                    self.assertEqual(entry.integrity, integrity_of(content, 16))
                for module in (MODULE_NAME, BRIDGE_MODULE, SOCKET_MODULE):
                    self.assertGreater(packed_entry(header.root, (module,)).size, 0)

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
            with patch("claudex.patch.build.SUPPORTED_RELEASES", (fixture,)):
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

if __name__ == "__main__":
    unittest.main()

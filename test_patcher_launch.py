"""Exercise clean desktop launch, pre-start fallback, and archive reuse."""

from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from desktop_environment import clean_environment, parse_desktop_environment
from patcher import build as archive_builder
from patcher.archive import Directory, Packed, encode_header, integrity_of
from patcher.build import Anchor, DesktopRelease
from patcher.launch import LaunchRefused, STOCK_LAUNCH_MARKER, cached_archive, launch, notify_stock_fallback, running_desktop


class DesktopEnvironmentTests(unittest.TestCase):
    def test_bootstrap_removes_agent_variables_and_private_path_entries(self) -> None:
        properties = {
            "PATH": "/usr/bin:/home/fixture/.codex/bin:/opt/agent/private:/tmp/codex-test/bin:./bin:",
            "CODEX_HOME": "/opt/agent",
            "CODEX_APP_TOOLS_PIPE_PATH": "fictitious socket",
            "CODEX_THREAD_ID": "fixture",
            "CODEX_SANDBOX_NETWORK_DISABLED": "1",
            "CODEX_CI": "1",
            "WAYLAND_DISPLAY": "wayland-fixture",
        }
        self.assertEqual(
            clean_environment(properties, Path("/home/fixture")),
            {"PATH": "/usr/bin", "HOME": "/home/fixture", "WAYLAND_DISPLAY": "wayland-fixture"},
        )

    def test_manager_environment_values_are_not_shell_evaluated(self) -> None:
        raw = json.dumps({"type": "as", "data": ["VALUE=spaces 'quotes' $(echo Run) = arg"]})
        self.assertEqual(
            parse_desktop_environment(raw, Path("/home/fixture"))["VALUE"],
            "spaces 'quotes' $(echo Run) = arg",
        )

    def test_manager_invalid_environment_entry_fails(self) -> None:
        with self.assertRaisesRegex(OSError, "malformed entry"):
            parse_desktop_environment('{"type":"as","data":["malformed"]}', Path("/home/fixture"))


class DesktopLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        stage = TemporaryDirectory()
        self.addCleanup(stage.cleanup)
        self.root = Path(stage.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.cache = self.root / "cache" / "claudex" / "desktop"
        self.state = self.root / ".local" / "state" / "claudex" / "desktop"
        self.stock = self.root / "stock.asar"
        self.stock.touch()
        self.patched = self.root / "patched.asar"
        self.record = self.root / "launch-record"
        self.notice_record = self.root / "notice-record"
        self.environment = {"HOME": str(self.root), "PATH": str(self.bin), "XDG_CACHE_HOME": str(self.root / "cache")}

    def executable(self, name: str, source: str) -> Path:
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n{source}\n")
        path.chmod(0o700)
        return path

    def fake_launcher(self, *, exit_code: int = 0) -> list[str]:
        launcher = self.executable(
            "fixture-account-launcher",
            f"""\
import json
import os
from pathlib import Path

with Path({str(self.record)!r}).open("a") as record:
    print(json.dumps({{
        "parent_codex_keys": sorted(key for key in os.environ if key.startswith("CODEX_")),
        "path": os.environ.get("PATH"),
        "patched_launch": "CLAUDEX_REMOTE_CONTROL_SINCE" in os.environ,
    }}), file=record)
raise SystemExit({exit_code})
""",
        )
        return [str(launcher)]

    def fake_bubblewrap(self) -> None:
        self.executable(
            "bwrap",
            """\
import os
import sys

arguments = sys.argv[sys.argv.index("--") + 1:]
os.execv(arguments[0], arguments)
""",
        )

    def fake_notifier(self, *, exit_code: int = 0, environment_reply: str = "") -> None:
        self.executable(
            "busctl",
            f"""\
import json
import os
from pathlib import Path
import sys

if "get-property" in sys.argv:
    print({environment_reply!r})
    raise SystemExit(0)
with Path({str(self.notice_record)!r}).open("a") as record:
    print(json.dumps({{
        "content": sys.argv[1:],
        "path": os.environ.get("PATH"),
        "agent_keys": [key for key in os.environ if key.startswith("CODEX_")],
    }}), file=record)
raise SystemExit({exit_code})
""",
        )

    def test_stock_cli_uses_clean_desktop_environment(self) -> None:
        args = self.fake_launcher()
        entries = [f"{key}={value}" for key, value in self.environment.items()]
        entries.extend(["DISPLAY=:999", "CODEX_CI=1"])
        self.fake_notifier(environment_reply=json.dumps({"type": "as", "data": entries}))
        with (
            patch("desktop_environment.SYSTEM_PATH", str(self.bin)),
            patch.dict(os.environ, {"CODEX_APP_TOOLS_PIPE_PATH": "fixture", "CODEX_THREAD_ID": "fixture"}),
            patch("patcher.launch.running_desktop", return_value=False),
            patch("patcher.launch.cached_archive") as cache,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(launch(self.stock, args, disabled=True), 0)
        cache.assert_not_called()
        payload = json.loads(self.record.read_text())
        self.assertEqual(payload, {
            "parent_codex_keys": [],
            "path": str(self.bin),
            "patched_launch": False,
        })
        self.assertTrue((self.state / STOCK_LAUNCH_MARKER).is_file())
        self.assertFalse(self.notice_record.exists())

    def test_nonzero_after_app_start_does_not_relaunch_stock(self) -> None:
        self.fake_bubblewrap()
        self.fake_notifier()
        args = self.fake_launcher(exit_code=23)
        with (
            patch("patcher.launch.desktop_environment", return_value=self.environment),
            patch("patcher.launch.running_desktop", return_value=False),
            patch("patcher.launch.cached_archive", return_value=self.patched),
        ):
            self.assertEqual(launch(self.stock, args, disabled=False), 23)
        self.assertEqual(len(self.record.read_text().splitlines()), 1)
        self.assertTrue(json.loads(self.record.read_text())["patched_launch"])
        self.assertFalse(self.notice_record.exists())

    def test_disabled_launch_works_with_an_unusable_archive_cache(self) -> None:
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text("not a directory")
        args = self.fake_launcher()
        with (
            patch("patcher.launch.desktop_environment", return_value=self.environment),
            patch("patcher.launch.running_desktop", return_value=False),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(launch(self.stock, args, disabled=True), 0)
        self.assertTrue((self.state / STOCK_LAUNCH_MARKER).is_file())

    def test_clean_exit_after_app_start_does_not_relaunch_stock(self) -> None:
        self.fake_bubblewrap()
        args = self.fake_launcher()
        with (
            patch("patcher.launch.desktop_environment", return_value=self.environment),
            patch("patcher.launch.running_desktop", return_value=False),
            patch("patcher.launch.cached_archive", return_value=self.patched),
        ):
            self.assertEqual(launch(self.stock, args, disabled=False), 0)
        self.assertEqual(len(self.record.read_text().splitlines()), 1)
        self.assertTrue(json.loads(self.record.read_text())["patched_launch"])

    def test_mount_failure_falls_back_to_stock_with_a_reason(self) -> None:
        self.executable("bwrap", "raise SystemExit(13)")
        args = self.fake_launcher()
        stderr = io.StringIO()
        with (
            patch("patcher.launch.desktop_environment", return_value=self.environment),
            patch("patcher.launch.running_desktop", return_value=False),
            patch("patcher.launch.cached_archive", return_value=self.patched),
            redirect_stderr(stderr),
        ):
            self.assertEqual(launch(self.stock, args, disabled=False), 0)
        self.assertIn("patch mount setup failed (exit 13). Launching stock Claude Desktop.", stderr.getvalue())
        self.assertFalse(json.loads(self.record.read_text())["patched_launch"])

    def test_build_failure_falls_back_to_stock_with_a_reason(self) -> None:
        self.fake_bubblewrap()
        self.fake_notifier()
        args = self.fake_launcher()
        stderr = io.StringIO()
        with (
            patch("patcher.launch.desktop_environment", return_value=self.environment),
            patch("patcher.launch.running_desktop", return_value=False),
            patch("patcher.launch.cached_archive", side_effect=ValueError("unsupported archive")),
            redirect_stderr(stderr),
        ):
            self.assertEqual(launch(self.stock, args, disabled=False), 0)
        self.assertIn("unsupported archive). Launching stock Claude Desktop.", stderr.getvalue())
        self.assertFalse(json.loads(self.record.read_text())["patched_launch"])
        self.assertEqual(json.loads(self.notice_record.read_text()), {
            "content": [
                "--user", "--auto-start=no", "--", "call", "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications", "org.freedesktop.Notifications", "Notify", "susssasa{sv}i",
                "Claudex", "0", "", "Claudex is launching stock Claude Desktop",
                "The patch could not start. Automatic Remote Control restore is off.", "0", "0", "-1",
            ],
            "path": str(self.bin),
            "agent_keys": [],
        })

    def test_notification_failure_leaves_stock_launch_status_intact(self) -> None:
        self.fake_notifier(exit_code=4)
        args = self.fake_launcher(exit_code=23)
        stderr = io.StringIO()
        with (
            patch("patcher.launch.desktop_environment", return_value=self.environment),
            patch("patcher.launch.running_desktop", return_value=False),
            redirect_stderr(stderr),
        ):
            self.assertEqual(launch(self.stock, args, disabled=False), 23)
        self.assertIn("bubblewrap unavailable", stderr.getvalue())
        self.assertTrue(self.notice_record.is_file())

    def test_notification_exec_error_or_timeout_does_not_escape(self) -> None:
        self.fake_notifier()
        for failure in (PermissionError("not executable"), subprocess.TimeoutExpired("busctl", 2)):
            with self.subTest(failure=failure), patch("patcher.launch.subprocess.run", side_effect=failure):
                notify_stock_fallback(self.environment)

    def test_account_exec_failure_before_app_start_falls_back(self) -> None:
        self.executable(
            "bwrap",
            """\
import os
import sys

arguments = sys.argv[sys.argv.index("--") + 1:]
arguments[6] = "/nonexistent/fixture-claude-launcher"
os.execv(arguments[0], arguments)
""",
        )
        args = self.fake_launcher()
        stderr = io.StringIO()
        with (
            patch("patcher.launch.desktop_environment", return_value=self.environment),
            patch("patcher.launch.running_desktop", return_value=False),
            patch("patcher.launch.cached_archive", return_value=self.patched),
            redirect_stderr(stderr),
        ):
            self.assertEqual(launch(self.stock, args, disabled=False), 0)
        self.assertIn("account launcher could not start inside the patch mount", stderr.getvalue())

    def test_running_desktop_refuses_an_implicit_handoff(self) -> None:
        args = self.fake_launcher()
        with (
            patch("patcher.launch.desktop_environment", return_value=self.environment),
            patch("patcher.launch.running_desktop", return_value=True),
            patch("patcher.launch.cached_archive") as build,
            self.assertRaisesRegex(LaunchRefused, "already running.*hand off"),
        ):
            launch(self.stock, args, disabled=False)
        build.assert_not_called()
        self.assertFalse(self.record.exists())

    def test_reuse_without_a_link_is_an_explicit_noop(self) -> None:
        args = self.fake_launcher()
        with (
            patch("patcher.launch.desktop_environment", return_value=self.environment),
            patch("patcher.launch.running_desktop", return_value=True),
            patch("patcher.launch.prune_cached_archives") as prune,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(launch(self.stock, args, disabled=False, reuse_running=True), 0)
        prune.assert_not_called()
        self.assertFalse(self.record.exists())

    def test_desktop_appearing_during_build_prevents_cache_pruning(self) -> None:
        self.fake_bubblewrap()
        args = self.fake_launcher()
        with (
            patch("patcher.launch.desktop_environment", return_value=self.environment),
            patch("patcher.launch.running_desktop", side_effect=[False, False, True]),
            patch("patcher.launch.cached_archive", return_value=self.patched),
            patch("patcher.launch.prune_cached_archives") as prune,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(launch(self.stock, args, disabled=False, reuse_running=True), 0)
        prune.assert_not_called()
        self.assertFalse(self.record.exists())

    def test_process_probe_distinguishes_electron_worker_from_main(self) -> None:
        proc = self.root / "proc"
        worker = proc / "111"
        worker.mkdir(parents=True)
        (worker / "comm").write_text("claude-desktop\n")
        (worker / "cmdline").write_bytes(b"/app/claude-desktop\0--type=renderer\0")
        self.assertFalse(running_desktop(proc))
        (worker / "cmdline").write_bytes(b"/app/claude-desktop\0")
        self.assertTrue(running_desktop(proc))

    def cache_fixture(self) -> DesktopRelease:
        chunk = b"stock anchor"
        self.stock.write_bytes(
            encode_header(Directory((("session.js", Packed(0, len(chunk), integrity_of(chunk, 16), None)),)))
            + chunk,
        )
        self.cache.mkdir(mode=0o700, parents=True)
        return DesktopRelease("fixture", hashlib.sha256(self.stock.read_bytes()).hexdigest(), (
            "session.js",
        ), (Anchor("fixture", chunk, b"patched anchor"),))

    def test_verified_cached_archive_avoids_rehash_and_rebuild(self) -> None:
        release = self.cache_fixture()
        with patch("patcher.build.SUPPORTED_RELEASES", (release,)):
            first = cached_archive(self.stock, self.cache)
            with (
                patch("patcher.build.identify_release", wraps=archive_builder.identify_release) as identify,
                patch("patcher.build.build", wraps=archive_builder.build) as build,
            ):
                self.assertEqual(cached_archive(self.stock, self.cache), first)
                identify.assert_not_called()
                build.assert_not_called()

    def test_source_change_invalidates_a_preexisting_cache(self) -> None:
        release = self.cache_fixture()
        with patch("patcher.build.SUPPORTED_RELEASES", (release,)):
            cached_archive(self.stock, self.cache)
            self.stock.write_bytes(b"unsupported archive")
            with self.assertRaisesRegex(ValueError, "unsupported Claude Desktop ASAR"):
                cached_archive(self.stock, self.cache)

    def test_changed_patch_inputs_get_a_different_cache_path(self) -> None:
        release = self.cache_fixture()
        with patch("patcher.build.SUPPORTED_RELEASES", (release,)):
            first = cached_archive(self.stock, self.cache)
            with patch("patcher.launch.patch_digest", return_value="different-patch"):
                self.assertNotEqual(cached_archive(self.stock, self.cache), first)

    def test_changed_cache_archive_is_rebuilt_before_use(self) -> None:
        release = self.cache_fixture()
        with patch("patcher.build.SUPPORTED_RELEASES", (release,)):
            original = cached_archive(self.stock, self.cache)
            original.chmod(0o600)
            original.write_bytes(b"tampered archive")
            rebuilt = cached_archive(self.stock, self.cache)
        self.assertNotEqual(rebuilt.read_bytes(), b"tampered archive")

    def test_launch_keeps_the_current_cache_and_preserves_unmanaged_entries(self) -> None:
        release = self.cache_fixture()
        self.fake_bubblewrap()
        args = self.fake_launcher()
        stale_patch = "a" * 64
        current_patch = "b" * 64
        with (
            patch("patcher.build.SUPPORTED_RELEASES", (release,)),
            patch("patcher.launch.patch_digest", return_value=stale_patch),
        ):
            stale = cached_archive(self.stock, self.cache)
        stale_reference, = self.cache.glob("*.json")

        staging = self.cache / "launch-unfinished"
        staging.mkdir()
        (staging / "app.asar").write_bytes(b"partially written")
        orphan = self.cache / f"{'c' * 64}-{stale_patch}.asar"
        orphan.write_bytes(b"no provenance")
        unrelated = self.cache / f"{'d' * 64}-{stale_patch}.json"
        unrelated.write_text("user note")
        linked = self.cache / f"{'e' * 64}-{stale_patch}.json"
        linked.symlink_to(unrelated)

        with (
            patch("patcher.build.SUPPORTED_RELEASES", (release,)),
            patch("patcher.launch.patch_digest", return_value=current_patch),
            patch("patcher.launch.desktop_environment", return_value=self.environment),
            patch("patcher.launch.running_desktop", return_value=False),
        ):
            self.assertEqual(launch(self.stock, args, disabled=False), 0)

        self.assertFalse(stale.exists())
        self.assertFalse(stale_reference.exists())
        self.assertEqual(len(list(self.cache.glob(f"*-{current_patch}.asar"))), 1)
        self.assertEqual(len(list(self.cache.glob(f"*-{current_patch}.json"))), 1)
        self.assertTrue((staging / "app.asar").is_file())
        self.assertTrue(orphan.exists())
        self.assertTrue(linked.is_symlink())
        self.assertEqual(unrelated.read_text(), "user note")

    def test_cache_hit_prunes_stale_references_to_the_current_archive(self) -> None:
        release = self.cache_fixture()
        self.fake_bubblewrap()
        args = self.fake_launcher()
        current_patch = "f" * 64
        with (
            patch("patcher.build.SUPPORTED_RELEASES", (release,)),
            patch("patcher.launch.patch_digest", return_value=current_patch),
            patch("patcher.launch.desktop_environment", return_value=self.environment),
            patch("patcher.launch.running_desktop", return_value=False),
        ):
            current = cached_archive(self.stock, self.cache)
            reference, = self.cache.glob("*.json")
            obsolete = self.cache / f"{'e' * 64}-{current_patch}.json"
            payload = json.loads(reference.read_text())
            payload["artifact"]["inode"] = -1
            obsolete.write_text(json.dumps(payload))
            with patch("patcher.build.build") as build:
                self.assertEqual(launch(self.stock, args, disabled=False), 0)
            build.assert_not_called()
        self.assertTrue(current.is_file())
        self.assertTrue(reference.is_file())
        self.assertFalse(obsolete.exists())


if __name__ == "__main__":
    unittest.main()

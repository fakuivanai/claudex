"""Build a separate ASAR from a supported Claude Desktop installation."""

import argparse
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile
from typing import BinaryIO

from .archive import (
    COPY_BLOCK, Header, PackedReplacement, blocks_from, encode_header,
    packed_entry, read_exact, read_header, replace_files,
)


MODULE_NAME = "claudex-restore.cjs"
BRIDGE_MODULE = "claudex-desktop-bridge.cjs"
SOCKET_MODULE = "claudex-desktop-socket.cjs"
NIX_STORE = Path("/nix/store")


@dataclass(frozen=True)
class Anchor:
    description: str
    stock: bytes
    patched: bytes


@dataclass(frozen=True)
class DesktopRelease:
    version: str
    digest: str
    session_chunk: tuple[str, ...]
    anchors: tuple[Anchor, ...]
    window_chunk: tuple[str, ...] | None = None


# These aliases are private to the pinned session-manager chunk.
# The reference locations are the stock manager's runLaunchPass, wB and gB.
STARTUP = (
    b"resumeQuitInterruptedSessionsAtLaunch(e){let t=this.launchPassChain.then((()=>"
    b"this.launchPass.runLaunchPass(e)));return this.launchPassChain=t.catch((()=>void 0)),t}"
)
STARTUP_HOOK = (
    b'resumeQuitInterruptedSessionsAtLaunch(e){'
    b'let claudexLaunchPass=this.launchPassChain.then(async()=>{'
    b'await this.launchPass.runLaunchPass(e);'
    b'await require("./claudex-restore.cjs").restoreAtLaunch(this,e,{'
    # t.I0() is the signed-in account read by the stock same-account restore.
    # t.ddt() is the fresh feature-flag check runLaunchPass makes after hydration.
    b'account:()=>t.I0(),flagsReady:()=>t.ddt(),'
    # gB subscribes to se.u, the organization rule store, until t.H1 elapses.
    # se.l() is the native availability blocker used by pB/wB.
    b'waitForOrganizationPolicy:()=>gB(t.H1,()=>se.l()==="org_denied"&&se.u.state()==="pending"),'
    # wB names t.ldt("4267904482") as ccd_remote_control_restore_at_launch_disabled.
    b'canRestore:async()=>se.l()===void 0&&!t.ldt("4267904482")'
    # Stock restoreBridgesAtLaunch requires this flag for saved user requests.
    b'&&t.ldt("976614668")'
    # runLaunchPass bounds the policy overlay readiness wait with t.ATt and t.H1.
    # The barrier t.DSt().overlayApplied() fails closed if the overlay isn't ready.
    b'&&await t.ATt(t.DSt().overlayApplied().then(()=>!0,()=>!1),t.H1,!1)'
    # se.o() is the managed-settings decision; stock requires "allowed".
    b'&&await se.o()==="allowed",'
    # Both stock quit and launch passes use t.vI to exclude unattended automation.
    b'isUnattended:chat=>t.vI(chat.sessionId,id=>this.sessions.get(id)),'
    # wS is the stock restore worktree/path safety probe, including its timeout.
    b'checkWorktree:chat=>wS(()=>this.worktreePool,chat),'
    # t.UTt sleeps for the stock warm-up slot; t.EEt is Desktop's main logger.
    b'wait:ms=>t.UTt(ms),log:message=>t.EEt.info(message)});'
    b'});return this.launchPassChain=claudexLaunchPass.catch(()=>void 0),claudexLaunchPass}'
)
DEFERRED_ROSTER = b"w.some((e=>e.interruptedByQuitAt!==void 0||e.remoteControlLiveAtUpdateQuit!==void 0))&&this.resumeQuitInterruptedSessionsAtLaunch(e)"
# w contains active chats discovered by Desktop's deferred archive scan.
DEFERRED_ROSTER_HOOK = DEFERRED_ROSTER.replace(
    b"e.interruptedByQuitAt",
    b"e.remoteControlUserEnabled===!0||e.interruptedByQuitAt",
    1,
)

# In zW.warmSession(e, r), e is the chat ID and i is its current roster entry.
# W is the spawned SDK query assigned to i.query before the native attachment
# installs it. R is the resolved spawn configuration passed to that call.
AFTER_WARM = b"this.deps.remoteControlLifecycle().installRemoteControlProcess(i,W,R.remoteControlReattaching);"
AFTER_WARM_HOOK = (
    AFTER_WARM
    # Native warmLifecycle.onWarmUp uses fromPage for a page-driven warm. The
    # interrupted-turn reopen path sets rowOpen when a person opens the row.
    # adoptOnly reattaches an existing SSH worker. allowFork is the explicit
    # rewind target that warmSession compares with pendingRewindTo.
    + b'if((r.fromPage===!0||r.rowOpen===!0)&&!r.adoptOnly&&r.allowFork===void 0){'
    b'void require("./claudex-restore.cjs").restoreAfterOpening('
    # LocalSessionManager constructs zW with accessors for its live session
    # table, native Remote Control lifecycle, and current account and org.
    b'this.deps.sessions(),i,W,this.deps.remoteControlLifecycle(),'
    # The warmer captured a.accountId/a.orgId before async work. Recheck that
    # tenant and the original roster entry at attach time.
    b'()=>a.accountId!==null&&t.I0()===a.accountId&&this.deps.currentAccountId()===a.accountId'
    b'&&this.deps.currentOrgId()===a.orgId&&this.deps.sessions().get(e)===i,'
    b'message=>t.EEt.info(message))}'
)
# LocalSessionManager.setSessionVisibility(n, r, i) takes a chat ID n and a
# visibility boolean r. It looks up a in the live session table and schedules
# native warm/preview visibility handling before this hook runs.
VISIBLE_CHAT = b"this.warmLifecycle.onVisibilityChange(n,r),this.previewIdleManager.onVisibilityChange(n,r),"
VISIBLE_CHAT_HOOK = (
    VISIBLE_CHAT
    # An idle installed query can reconnect when shown. Native hydration writes
    # homeTenancy from the account/org that owns this chat, even across switches.
    + b'(r&&a.query&&!a.isRunning&&a.homeTenancy&&require("./claudex-restore.cjs").restoreAfterOpening('
    # The manager holds the same table and lifecycle exposed through zW's deps.
    b'this.sessions,a,a.query,this.remoteControlLifecycle,'
    # Recheck that the manager is active and that the same chat still belongs
    # to the signed-in account and current organization before attaching.
    b'()=>!this.appQuitting&&this.sessions.get(n)===a&&a.homeTenancy.accountId!==null'
    b'&&t.I0()===a.homeTenancy.accountId&&this.currentAccountId===a.homeTenancy.accountId'
    b'&&this.currentOrgId===a.homeTenancy.orgId,message=>t.EEt.info(message))),'
)

# Ei(e, y, C) is the renderer's native LocalSessions factory. The main
# process passes its window WebContents wrapper, its local session manager,
# and the window dispatcher. t.I0()/t.EEt belong to the main process's signed-in
# account provider and logger. Preserve the factory object original callers use.
WINDOW_API = Anchor(
    "LocalSessions window API factory",
    b"exports.createLocalSessionsApi=Ei;",
    b"exports.createLocalSessionsApi=function(window,manager,dispatcher){"
    b"const api=Ei(window,manager,dispatcher);"
    b'try{require("./claudex-desktop-bridge.cjs").attachDesktopWindow('
    b'window,api,manager,()=>t.I0(),message=>t.EEt.info(message),async()=>({'
    # The first-party renderer stores start picks in AppPreferences.epitaxyPrefs.
    # The pinned main process owns its feature flag and managed Auto checks.
    b'preferences:t.Udt("epitaxyPrefs"),autoDefaultEnabled:t.udt("ccd_auto_default_mode",false),'
    b'autoAllowed:!t.tm("code")&&!t.jdt()&&!t.bZ(await t.hZ())}))}'
    b'catch{t.EEt.warn("[claudex] Could not attach the Desktop control socket")}return api};',
)


SUPPORTED_RELEASES = (
    DesktopRelease(
        version="5.11492.5",
        digest="33a8a05f5704a7159711c7aa9ab99086ea2b04f17ac9e494fb1df54c19a142e6",
        session_chunk=(".vite", "build", "index.chunk-DkwynUE7.js"),
        window_chunk=(".vite", "build", "index.chunk-DKhxeID8.js"),
        anchors=(
            Anchor("startup pass", STARTUP, STARTUP_HOOK),
            Anchor("deferred active roster", DEFERRED_ROSTER, DEFERRED_ROSTER_HOOK),
            Anchor("resumed worker installed", AFTER_WARM, AFTER_WARM_HOOK),
            Anchor("existing worker shown", VISIBLE_CHAT, VISIBLE_CHAT_HOOK),
        ),
    ),
)


def hash_archive(source: BinaryIO) -> str:
    source.seek(0)
    digest = hashlib.sha256()
    for data in iter(lambda: source.read(COPY_BLOCK), b""):
        digest.update(data)
    return digest.hexdigest()


def identify_release(source: BinaryIO) -> DesktopRelease:
    digest = hash_archive(source)
    for release in SUPPORTED_RELEASES:
        if release.digest == digest:
            return release
    versions = ", ".join(release.version for release in SUPPORTED_RELEASES)
    raise ValueError(f"unsupported Claude Desktop ASAR SHA-256 {digest}; supported version: {versions}")


def apply_anchors(original: bytes, release: DesktopRelease, anchors: Sequence[Anchor]) -> bytes:
    result = original
    for anchor in anchors:
        if result.count(anchor.stock) != 1:
            raise ValueError(f"{release.version} reconnect patch anchor changed: {anchor.description}")
        result = result.replace(anchor.stock, anchor.patched, 1)
    return result


def patch_chunk(original: bytes, release: DesktopRelease) -> bytes:
    return apply_anchors(original, release, release.anchors)


def source_chunk(source: BinaryIO, header: Header, path: tuple[str, ...]) -> PackedReplacement:
    original = packed_entry(header.root, path)
    source.seek(header.body_offset + original.offset)
    return PackedReplacement(path, original, read_exact(source, original.size))


def patched_blocks(
    source: BinaryIO, body_offset: int, body_size: int, edited: Sequence[PackedReplacement],
    header: bytes, modules: Sequence[bytes],
) -> Iterator[bytes]:
    yield header
    offset = 0
    for change in edited:
        yield from blocks_from(source, body_offset + offset, change.before.offset - offset)
        yield change.content
        offset = change.end
    yield from blocks_from(source, body_offset + offset, body_size - offset)
    yield from modules


def build(source_path: Path, output_path: Path) -> str:
    source_path = source_path.resolve(strict=True)
    with source_path.open("rb") as source:
        initial = os.fstat(source.fileno())
        release = identify_release(source)
        header = read_header(source)
        session = source_chunk(source, header, release.session_chunk)
        edited = [PackedReplacement(session.path, session.before, patch_chunk(session.content, release))]
        modules = {(*session.path[:-1], MODULE_NAME): Path(__file__).with_name("restore.cjs").read_bytes()}
        if release.window_chunk is not None:
            window = source_chunk(source, header, release.window_chunk)
            edited.append(PackedReplacement(
                window.path, window.before, apply_anchors(window.content, release, (WINDOW_API,)),
            ))
            modules[(*window.path[:-1], BRIDGE_MODULE)] = Path(__file__).with_name("bridge.cjs").read_bytes()
            modules[(*window.path[:-1], SOCKET_MODULE)] = Path(__file__).with_name("socket.cjs").read_bytes()
        return publish_archive(source_path, source, initial, header, edited, modules, output_path)


def publish_archive(
    source_path: Path,
    source: BinaryIO,
    initial: os.stat_result,
    header: Header,
    replacements: Sequence[PackedReplacement],
    added_files: dict[tuple[str, ...], bytes],
    output_path: Path,
) -> str:
    """Publish a complete patched archive without replacing an existing file."""
    body_size = initial.st_size - header.body_offset
    edited = sorted(replacements, key=lambda change: change.before.offset)
    new_header = encode_header(replace_files(header.root, edited, body_size, added_files))
    with tempfile.TemporaryDirectory(dir=output_path.parent) as directory:
        temporary = Path(directory) / "app.asar"
        digest = hashlib.sha256()
        with temporary.open("wb") as candidate:
            blocks = patched_blocks(
                source, header.body_offset, body_size, edited, new_header, tuple(added_files.values()),
            )
            for block in blocks:
                candidate.write(block)
                digest.update(block)
        if not source_path.is_relative_to(NIX_STORE):
            current = os.fstat(source.fileno())
            if (current.st_mtime_ns, current.st_ctime_ns) != (initial.st_mtime_ns, initial.st_ctime_ns):
                raise OSError("The source archive changed during the build.")
        os.link(temporary, output_path)
        return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="installed app.asar")
    parser.add_argument("--output", required=True, type=Path, help="new app.asar path; never overwritten")
    arguments = parser.parse_args()
    source: Path = arguments.source
    output: Path = arguments.output
    try:
        digest = build(source, output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"ASAR build refused: {error}\n")
    print(f"{digest}  {output}")


if __name__ == "__main__":
    main()

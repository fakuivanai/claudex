"""Build a separate ASAR from a supported Claude Desktop installation."""

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile
from typing import BinaryIO

from .archive import COPY_BLOCK, blocks_from, encode_header, integrity_of, packed_entry, read_exact, read_header, replace_file


MODULE_NAME = "claudex-restore.cjs"


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


# The local session manager serializes launch passes and records account
# changes in its load epoch. The warmer hook runs only after the resumed
# query is installed. These aliases are private to this exact archive.
STARTUP = (
    b"resumeQuitInterruptedSessionsAtLaunch(e){let t=this.launchPassChain.then((()=>"
    b"this.launchPass.runLaunchPass(e)));return this.launchPassChain=t.catch((()=>void 0)),t}"
)
STARTUP_HOOK = (
    b'resumeQuitInterruptedSessionsAtLaunch(e){'
    b'let claudexLaunchPass=this.launchPassChain.then(async()=>{'
    b'await this.launchPass.runLaunchPass(e);'
    b'await require("./claudex-restore.cjs").restoreAtLaunch(this,e,{'
    b'account:()=>t.I0(),flagsReady:()=>t.ddt(),'
    b'waitForOrganizationPolicy:()=>gB(t.H1,()=>se.l()==="org_denied"&&se.u.state()==="pending"),'
    b'canRestore:async()=>se.l()===void 0&&!t.ldt("4267904482")'
    b'&&await t.ATt(t.DSt().overlayApplied().then(()=>!0,()=>!1),t.H1,!1)'
    b'&&await se.o()==="allowed",'
    b'hasPendingInput:chat=>t.vI(chat.sessionId,id=>this.sessions.get(id)),'
    b'checkWorktree:chat=>wS(()=>this.worktreePool,chat),'
    b'wait:ms=>t.UTt(ms),log:message=>t.EEt.info(message)});'
    b'});return this.launchPassChain=claudexLaunchPass.catch(()=>void 0),claudexLaunchPass}'
)
DEFERRED_ROSTER = b"w.some((e=>e.interruptedByQuitAt!==void 0||e.remoteControlLiveAtUpdateQuit!==void 0))&&this.resumeQuitInterruptedSessionsAtLaunch(e)"
DEFERRED_ROSTER_HOOK = DEFERRED_ROSTER.replace(
    b"!==void 0))&&",
    b'!==void 0||require("./claudex-restore.cjs").hasSavedRemoteControl(e)))&&',
    1,
)
AFTER_WARM = b"this.deps.remoteControlLifecycle().installRemoteControlProcess(i,W,R.remoteControlReattaching);"
AFTER_WARM_HOOK = (
    AFTER_WARM
    + b'if((r.fromPage===!0||r.rowOpen===!0)&&!r.adoptOnly&&r.allowFork===void 0){'
    b'void require("./claudex-restore.cjs").restoreAfterOpening('
    b'i,W,this.deps.remoteControlLifecycle(),'
    b'()=>a.accountId!==null&&t.I0()===a.accountId&&this.deps.currentAccountId()===a.accountId'
    b'&&this.deps.currentOrgId()===a.orgId&&this.deps.sessions().get(e)===i,'
    b'message=>t.EEt.info(message))}'
)
VISIBLE_CHAT = b"this.warmLifecycle.onVisibilityChange(n,r),this.previewIdleManager.onVisibilityChange(n,r),"
VISIBLE_CHAT_HOOK = (
    VISIBLE_CHAT
    + b'(r&&a.query&&!a.isRunning&&a.homeTenancy&&require("./claudex-restore.cjs").restoreAfterOpening('
    b'a,a.query,this.remoteControlLifecycle,'
    b'()=>!this.appQuitting&&this.sessions.get(n)===a&&a.homeTenancy.accountId!==null'
    b'&&t.I0()===a.homeTenancy.accountId&&this.currentAccountId===a.homeTenancy.accountId'
    b'&&this.currentOrgId===a.homeTenancy.orgId,message=>t.EEt.info(message))),'
)

SUPPORTED_RELEASES = (
    DesktopRelease(
        version="5.11492.5",
        digest="33a8a05f5704a7159711c7aa9ab99086ea2b04f17ac9e494fb1df54c19a142e6",
        session_chunk=(".vite", "build", "index.chunk-DkwynUE7.js"),
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


def patch_chunk(original: bytes, release: DesktopRelease) -> bytes:
    if MODULE_NAME.encode() in original:
        raise ValueError("source chunk already contains the reconnect patch")
    result = original
    for anchor in release.anchors:
        if result.count(anchor.stock) != 1:
            raise ValueError(f"{release.version} reconnect patch anchor changed: {anchor.description}")
        result = result.replace(anchor.stock, anchor.patched, 1)
    return result


def verify_candidate(
    source: BinaryIO, candidate: BinaryIO, original_body: int, start: int, end: int, tail_size: int,
    encoded_header: bytes, replacement: bytes, module: bytes,
) -> str:
    candidate.flush()
    candidate.seek(0)
    if read_exact(candidate, len(encoded_header)) != encoded_header:
        raise ValueError("patched ASAR header did not round-trip")
    for data in blocks_from(source, original_body, start):
        if read_exact(candidate, len(data)) != data:
            raise ValueError("unmodified ASAR bytes changed before the patch")
    if read_exact(candidate, len(replacement)) != replacement:
        raise ValueError("patched ASAR chunk did not round-trip")
    for data in blocks_from(source, original_body + end, tail_size):
        if read_exact(candidate, len(data)) != data:
            raise ValueError("unmodified ASAR bytes changed after the patch")
    if read_exact(candidate, len(module)) != module or candidate.read(1):
        raise ValueError("embedded reconnect module did not round-trip")
    return hash_archive(candidate)


def build(source_path: Path, output_path: Path) -> str:
    module = Path(__file__).with_name("restore.cjs").read_bytes()
    with source_path.open("rb") as source:
        release = identify_release(source)
        header = read_header(source)
        body_size = os.fstat(source.fileno()).st_size - header.body_offset
        target = packed_entry(header.root, release.session_chunk)
        start, end = target.offset, target.offset + target.size
        source.seek(header.body_offset + start)
        original = read_exact(source, target.size)
        if target.integrity != integrity_of(original, target.integrity.block_size):
            raise ValueError("source ASAR chunk integrity did not match its header")
        replacement = patch_chunk(original, release)
        new_root = replace_file(header.root, release.session_chunk, target, replacement, body_size, MODULE_NAME, module)
        encoded_header = encode_header(new_root)
        tail_size = body_size - end

        # Publish only a complete new archive. Hard linking refuses to
        # overwrite either the installed archive or a previous build.
        with tempfile.TemporaryDirectory(dir=output_path.parent) as directory:
            temporary = Path(directory) / "app.asar"
            with temporary.open("w+b") as candidate:
                candidate.write(encoded_header)
                candidate.writelines(blocks_from(source, header.body_offset, start))
                candidate.write(replacement)
                candidate.writelines(blocks_from(source, header.body_offset + end, tail_size))
                candidate.write(module)
                digest = verify_candidate(
                    source, candidate, header.body_offset, start, end, tail_size, encoded_header, replacement, module,
                )
            os.link(temporary, output_path)
            return digest


@dataclass
class Arguments(argparse.Namespace):
    source: Path
    output: Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="installed app.asar")
    parser.add_argument("--output", required=True, type=Path, help="new app.asar path; never overwritten")
    arguments = parser.parse_args(namespace=Arguments(Path(), Path()))
    try:
        digest = build(arguments.source, arguments.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"ASAR build refused: {error}\n")
    print(f"{digest}  {arguments.output}")


if __name__ == "__main__":
    main()

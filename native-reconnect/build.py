#!/usr/bin/env python3
"""Build a separate, verified ASAR for the pinned Claude Desktop package."""

import argparse
import hashlib
import json
import os
import struct
import tempfile
from pathlib import Path


SOURCE_SHA256 = "33a8a05f5704a7159711c7aa9ab99086ea2b04f17ac9e494fb1df54c19a142e6"
CHUNK = "index.chunk-DOKiptke.js"
MODULE = "thread-bridge-reconnect.cjs"
FLAG = "--thread-bridge-reconnect"
NONCE_KEY = "threadBridgeReconnectNonce"
HEADER_LIMIT = 4 * 1024 * 1024
COPY_BLOCK = 1024 * 1024

# Aliases in the shipped minified chunk are version-specific. kC loads the
# genuine Desktop LocalSessionManager. The normal listener handles all other
# arguments exactly as it did before. The update listener ignores only this
# reserved request, so a send during shutdown cannot request later focus.
HELPERS = (
    f'function threadBridgeParseNonce(e){{return typeof e==="string"&&e.length==={len(FLAG) + 1 + 36}&&'
    f'/^{FLAG}=[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$/.test(e)'
    f'?e.slice({len(FLAG) + 1}):null}}'
    f'function threadBridgeReserved(e){{return typeof e==="string"&&'
    f'(e==="{FLAG}"||e.startsWith("{FLAG}="))}}'
    f'function threadBridgeReconnectArgv(e){{return Array.isArray(e)&&e.some('
    'threadBridgeReserved)}'
    'function threadBridgeStartupNonce(e){return Array.isArray(e)&&e.length===2&&'
    'typeof e[0]==="string"&&e[0].startsWith("/")?threadBridgeParseNonce(e[1]):null}'
    'function threadBridgeReceivedNonce(e,t){if(!Array.isArray(e))return null;'
    'let r=e.filter(threadBridgeReserved),n=r.length===1?threadBridgeParseNonce(r[0]):null;'
    f'return n!==null&&t?.{NONCE_KEY}===n?n:null}}'
).encode()
DISPATCH = (
    f'if(threadBridgeReconnectArgv(n)||threadBridgeData?.{NONCE_KEY}!==void 0){{'
    'let nonce=threadBridgeReceivedNonce(n,threadBridgeData);if(nonce!==null){'
    'void kC().then(async e=>{await e.waitForSessionsLoaded();'
    f'await require("./{MODULE}").handleRequest([process.execPath,"{FLAG}="+nonce],e)}}).catch(()=>{{'
    'P.warn("[thread-bridge] Remote Control request could not be handled")})}'
    'return}'
).encode()
UPDATE_HOOK = b'u.app.on("second-instance",((e,t)=>{t.includes("--startup")||ZP()}))'
MAIN_HOOK = b'u.app.on("second-instance",((t,n,r)=>{if(bRi(),$P())return;'
LOCK_HOOK = (
    b'function qXo(){let e=u.app.getPath("userData"),t=n.default.join(e,bu);'
    b'if(!GXo(e))return KXo(e),u.app.requestSingleInstanceLock();'
    b'let r=T7(e);if(C7(t)!==null&&UXo(e,r))return XXo(e),!1;'
    b'try{return u.app.requestSingleInstanceLock()}finally{BXo()}}'
)
LOCK_BRANCH = (
    'if(reconnectNonce!==void 0){'
    'if(GXo(e)||a.default.existsSync(n.default.join(e,xu)))return false;'
    f'return u.app.requestSingleInstanceLock({{{NONCE_KEY}:reconnectNonce}})}}'
).encode()
STARTUP_HOOK = (
    b'if(e&&(P.warn("startup: oversized deep-link argv; relaunching with it capped"),'
    b'u.app.relaunch({args:Ccr([...e,mZo])}),u.app.exit(0)),!(!e&&!jas&&qXo()))u.app.quit();'
)
STARTUP_BRANCH = (
    b'if(threadBridgeReconnectArgv(c.default.argv)){'
    b'let nonce=threadBridgeStartupNonce(c.default.argv);'
    b'try{if(nonce!==null&&!jas&&qXo(nonce))u.app.releaseSingleInstanceLock()}catch{}'
    b'u.app.exit(0)}else '
)
PATCHES = (
    (b"function ZP(){", HELPERS + b"function ZP(){"),
    (
        UPDATE_HOOK,
        UPDATE_HOOK.replace(b"((e,t)=>", b"((e,t,r,threadBridgeData)=>").replace(
            b"||ZP()",
            f"||threadBridgeReconnectArgv(t)||threadBridgeData?.{NONCE_KEY}!==void 0||ZP()".encode(),
        ),
    ),
    (MAIN_HOOK, MAIN_HOOK.replace(b"((t,n,r)=>", b"((t,n,r,threadBridgeData)=>") + DISPATCH),
    (
        LOCK_HOOK,
        LOCK_HOOK.replace(b"qXo()", b"qXo(reconnectNonce)").replace(
            b"join(e,bu);", b"join(e,bu);" + LOCK_BRANCH,
        ),
    ),
    (STARTUP_HOOK, STARTUP_BRANCH + STARTUP_HOOK),
)


def read_exact(file, size):
    data = file.read(size)
    if len(data) != size:
        raise ValueError("truncated ASAR")
    return data


def read_header(file):
    file.seek(0)
    first, total, payload, length = struct.unpack("<4I", read_exact(file, 16))
    padding = (-length) % 4
    if (
        first != 4
        or length > HEADER_LIMIT
        or payload != length + padding + 4
        or total != payload + 4
    ):
        raise ValueError("unexpected ASAR pickle header")
    header = json.loads(read_exact(file, length))
    if read_exact(file, padding) != bytes(padding):
        raise ValueError("unexpected ASAR header padding")
    return 8 + total, header


def encode_header(header):
    data = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padding = (-len(data)) % 4
    payload = len(data) + padding + 4
    return struct.pack("<4I", 4, payload + 4, payload, len(data)) + data + bytes(padding)


def file_entries(children):
    for entry in children.values():
        if "files" in entry:
            yield from file_entries(entry["files"])
        else:
            yield entry


def integrity(data, block_size):
    return {
        "algorithm": "SHA256",
        "hash": hashlib.sha256(data).hexdigest(),
        "blockSize": block_size,
        "blocks": [
            hashlib.sha256(data[start : start + block_size]).hexdigest()
            for start in range(0, len(data), block_size)
        ],
    }


def patch_chunk(original):
    if b"threadBridgeReconnectArgv" in original or FLAG.encode() in original:
        raise ValueError("archive already contains a reconnect hook")
    bare_lock = b"u.app.requestSingleInstanceLock()"
    if LOCK_HOOK.count(bare_lock) != 2 or original.count(bare_lock) != 2:
        raise ValueError("stock single-instance lock calls changed")
    patched = original
    for anchor, replacement in PATCHES:
        if original.count(anchor) != 1:
            raise ValueError(f"reconnect hook anchor count changed: {anchor!r}")
        patched = patched.replace(anchor, replacement, 1)
    return patched


def validate_and_shift(header, target, old_body_size, delta):
    start = int(target["offset"])
    end = start + target["size"]
    for entry in file_entries(header["files"]):
        if "offset" not in entry:
            continue
        offset = int(entry["offset"])
        length = entry["size"]
        if entry.get("unpacked") or offset < 0 or length < 0 or offset + length > old_body_size:
            raise ValueError("invalid packed entry location")
        if entry is target:
            continue
        if length and offset < end and offset + length > start:
            raise ValueError("hook chunk overlaps or aliases a second packed entry")
        # The source archive deduplicates several other files by offset.
        # Shifting each reference preserves those aliases and their content.
        if offset >= end:
            entry["offset"] = str(offset + delta)


def copy_range(source, output, offset, length):
    source.seek(offset)
    while length:
        data = read_exact(source, min(length, COPY_BLOCK))
        output.write(data)
        length -= len(data)


def verify_range(source, candidate, offset, length):
    source.seek(offset)
    while length:
        data = read_exact(source, min(length, COPY_BLOCK))
        if read_exact(candidate, len(data)) != data:
            raise ValueError("unmodified packed content no longer matches the original")
        length -= len(data)


def verify_candidate(source, candidate, header, source_body, start, end, tail_size, patched, adapter):
    candidate.flush()
    body, actual = read_header(candidate)
    if actual != header:
        raise ValueError("constructed header did not round-trip")
    candidate.seek(body)
    verify_range(source, candidate, source_body, start)
    if read_exact(candidate, len(patched)) != patched:
        raise ValueError("patched chunk read-back failed")
    verify_range(source, candidate, source_body + end, tail_size)
    if read_exact(candidate, len(adapter)) != adapter or candidate.read(1):
        raise ValueError("embedded adapter read-back failed")
    candidate.seek(0)
    return hashlib.file_digest(candidate, "sha256").hexdigest()


def build(source_path, adapter_path, output_path):
    adapter = adapter_path.read_bytes()
    if not adapter or len(adapter) > 128 * 1024:
        raise ValueError("expected a nonempty adapter no larger than 128 KiB")
    adapter.decode("utf-8")
    with source_path.open("rb") as source:
        actual_source_hash = hashlib.file_digest(source, "sha256").hexdigest()
        if actual_source_hash != SOURCE_SHA256:
            raise ValueError(f"unsupported Claude Desktop ASAR SHA-256: {actual_source_hash}")
        source_body, header = read_header(source)
        old_body_size = os.fstat(source.fileno()).st_size - source_body
        children = header["files"][".vite"]["files"]["build"]["files"]
        if MODULE in children:
            raise ValueError("reconnect adapter already exists in the source")
        target = children[CHUNK]
        start = int(target["offset"])
        end = start + target["size"]
        source.seek(source_body + start)
        original = read_exact(source, target["size"])
        block_size = target["integrity"]["blockSize"]
        if target["integrity"] != integrity(original, block_size):
            raise ValueError("source hook chunk integrity did not match metadata")
        patched = patch_chunk(original)
        delta = len(patched) - len(original)
        validate_and_shift(header, target, old_body_size, delta)
        target.update(size=len(patched), integrity=integrity(patched, block_size))
        children[MODULE] = {
            "size": len(adapter),
            "offset": str(old_body_size + delta),
            "integrity": integrity(adapter, block_size),
        }

        # Publish only a complete, verified file. link refuses to replace an
        # existing file (including the pinned source or a symlink to it).
        with tempfile.NamedTemporaryFile(dir=output_path.parent) as candidate:
            candidate.write(encode_header(header))
            copy_range(source, candidate, source_body, start)
            candidate.write(patched)
            copy_range(source, candidate, source_body + end, old_body_size - end)
            candidate.write(adapter)
            output_hash = verify_candidate(
                source, candidate, header, source_body, start, end, old_body_size - end, patched, adapter
            )
            os.link(candidate.name, output_path)
    return output_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--adapter", default=Path(__file__).with_name("adapter.cjs"), type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new app.asar path; never overwritten")
    arguments = parser.parse_args()
    try:
        output_hash = build(arguments.source, arguments.adapter, arguments.output)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"ASAR build refused: {error}\n")
    print(f"{output_hash}  {arguments.output}")


if __name__ == "__main__":
    main()

"""Add headless peer preparation to the pinned Codex Desktop host manager."""

import argparse
import os
from pathlib import Path

from claudex.patch.archive import PackedReplacement, read_header
from claudex.patch.build import hash_archive, publish_archive, source_chunk


VERSION = "26.1103.60933"
SOURCE_DIGEST = "0cf158d7fa96f1f250acddd1f7dd9a99ad40537973e3ec117309a630945cfff4"
HOST_MANAGER = (".vite", "build", "early-bootstrap-815-e030c49be4b9c405.js")
PREPARE_MODULE = "claudex-codex-prepare.cjs"

# Installed Codex Desktop 26.1103.60933, host coordination class ET.bind.
# Register alongside the existing thread owner and share that binding's lifetime.
OWNER_BINDING = (
    b'this.removeThreadOwner=(0,ed.ZRf)({hostId:t,ipcClient:this.session.client,'
    b'canHandle:({conversationId:e},t)=>r(e,t),discoverOwner:r,'
    b'requestThreadFollower:async({request:t})=>(this.assertCurrent(),e.handleThreadFollowerRequest(t))})'
)
PREPARE_BINDING = (
    OWNER_BINDING
    + b';if(t==="local"){'
    + b'let removeOwner=this.removeThreadOwner,'
    + b'removePrepare=require("./claudex-codex-prepare.cjs").registerPrepareThread({'
    + b'host:e,ipcClient:this.session.client,assertCurrent:()=>this.assertCurrent()});'
    + b'this.removeThreadOwner=()=>{try{removePrepare()}finally{removeOwner()}}}'
)


def patch_host_manager(contents: bytes) -> bytes:
    if contents.count(OWNER_BINDING) != 1:
        raise ValueError("The Codex Desktop host owner binding did not match the pinned release.")
    return contents.replace(OWNER_BINDING, PREPARE_BINDING, 1)


def build(source_path: Path, output_path: Path) -> str:
    source_path = source_path.resolve(strict=True)
    with source_path.open("rb") as source:
        initial = os.fstat(source.fileno())
        if hash_archive(source) != SOURCE_DIGEST:
            raise ValueError(f"The installed Codex Desktop ASAR does not match {VERSION}.")
        header = read_header(source)
        manager = source_chunk(source, header, HOST_MANAGER)
        edit = PackedReplacement(manager.path, manager.before, patch_host_manager(manager.content))
        modules = {(*manager.path[:-1], PREPARE_MODULE): Path(__file__).with_name("prepare.cjs").read_bytes()}
        return publish_archive(source_path, source, initial, header, [edit], modules, output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="installed Codex Desktop app.asar")
    parser.add_argument("--output", type=Path, required=True, help="new archive path, never overwritten")
    arguments = parser.parse_args()
    try:
        digest = build(arguments.source, arguments.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Codex Desktop patch refused: {error}\n")
    print(f"{digest}  {arguments.output}")


if __name__ == "__main__":
    main()

"""Patch peer delivery and turn presentation in the pinned Codex Desktop."""

import argparse
import os
from pathlib import Path

from claudex.patch.archive import PackedReplacement, read_header
from claudex.patch.build import hash_archive, publish_archive, source_chunk


VERSION = "26.1103.60933"
SOURCE_DIGEST = "0cf158d7fa96f1f250acddd1f7dd9a99ad40537973e3ec117309a630945cfff4"
HOST_MANAGER = (".vite", "build", "early-bootstrap-815-e030c49be4b9c405.js")
RENDERER_MANAGER = ("webview", "assets", "async", "182735.5b42af8792.js")
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


# The native start path creates an optimistic row even for tool-only input.
# On a join, Ig/a1 selects the existing turn first and leaves that row anonymous.
# The acknowledgement can safely retire it using the same native helper as
# turn/addUserMessage admission. Both owner implementations can receive a send.
HOST_TURN_ACK = b"let n=null==q?Ig(r,s,m.turn.id):null;"
HOST_TURN_ACK_PATCH = (
    HOST_TURN_ACK
    + b'if(n&&m.turn.id!=null&&n.turnId===m.turn.id&&S.toolOutput?.namespace==="claudex"'
    + b'&&S.toolOutput.name==="peer_message"){'
    + b'const claudexPendingTurn=Ig(r,s,null);'
    + b'if(claudexPendingTurn&&claudexPendingTurn!==n)(0,bA.Oc)(r,claudexPendingTurn)}'
)
RENDERER_TURN_ACK = b"let r=null==ei?a1(n,a,f.turn.id):null;"
RENDERER_TURN_ACK_PATCH = (
    RENDERER_TURN_ACK
    + b'if(r&&f.turn.id!=null&&r.turnId===f.turn.id&&T.toolOutput?.namespace==="claudex"'
    + b'&&T.toolOutput.name==="peer_message"){'
    + b'const claudexPendingTurn=a1(n,a,null);'
    + b'if(claudexPendingTurn&&claudexPendingTurn!==r)(0,e5.Oc)(n,claudexPendingTurn)}'
)


def patch_host_manager(contents: bytes) -> bytes:
    if contents.count(OWNER_BINDING) != 1:
        raise ValueError("The Codex Desktop host owner binding did not match the pinned release.")
    if contents.count(HOST_TURN_ACK) != 1:
        raise ValueError("The Codex Desktop host turn acknowledgement did not match the pinned release.")
    return contents.replace(OWNER_BINDING, PREPARE_BINDING, 1).replace(HOST_TURN_ACK, HOST_TURN_ACK_PATCH, 1)


def patch_renderer_manager(contents: bytes) -> bytes:
    if contents.count(RENDERER_TURN_ACK) != 1:
        raise ValueError("The Codex Desktop renderer turn acknowledgement did not match the pinned release.")
    return contents.replace(RENDERER_TURN_ACK, RENDERER_TURN_ACK_PATCH, 1)


def build(source_path: Path, output_path: Path) -> str:
    source_path = source_path.resolve(strict=True)
    with source_path.open("rb") as source:
        initial = os.fstat(source.fileno())
        if hash_archive(source) != SOURCE_DIGEST:
            raise ValueError(f"The installed Codex Desktop ASAR does not match {VERSION}.")
        header = read_header(source)
        manager = source_chunk(source, header, HOST_MANAGER)
        renderer = source_chunk(source, header, RENDERER_MANAGER)
        edits = [
            PackedReplacement(manager.path, manager.before, patch_host_manager(manager.content)),
            PackedReplacement(renderer.path, renderer.before, patch_renderer_manager(renderer.content)),
        ]
        modules = {(*manager.path[:-1], PREPARE_MODULE): Path(__file__).with_name("prepare.cjs").read_bytes()}
        return publish_archive(source_path, source, initial, header, edits, modules, output_path)


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

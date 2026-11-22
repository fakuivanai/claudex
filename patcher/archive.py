"""Read and rewrite the Electron ASAR format used by the pinned package."""

from collections.abc import Iterator
from dataclasses import dataclass, replace
import hashlib
import json
import struct
from typing import BinaryIO


# The verified ASAR header is small relative to the archive body.
HEADER_LIMIT = 4 * 1024 * 1024
# Stream the body instead of keeping an entire installed archive in memory.
COPY_BLOCK = 1024 * 1024


@dataclass(frozen=True)
class Integrity:
    digest: str
    block_size: int
    blocks: tuple[str, ...]


@dataclass(frozen=True)
class Packed:
    offset: int
    size: int
    integrity: Integrity
    executable: bool | None


@dataclass(frozen=True)
class Unpacked:
    size: int
    integrity: Integrity


@dataclass(frozen=True)
class Directory:
    children: tuple[tuple[str, "Entry"], ...]


Entry = Packed | Unpacked | Directory


@dataclass(frozen=True)
class Header:
    body_offset: int
    root: Directory


def read_exact(file: BinaryIO, size: int) -> bytes:
    data = file.read(size)
    if len(data) != size:
        raise ValueError("truncated ASAR")
    return data


def require_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an ASAR header object")
    output: dict[str, object] = {}
    for key, child in value.items():
        if not isinstance(key, str):
            raise ValueError("invalid ASAR object key")
        output[key] = child
    return output


def require_size(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid ASAR size")
    return value


def require_digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("invalid ASAR SHA-256 digest")
    return value


def parse_integrity(value: object) -> Integrity:
    fields = require_object(value)
    blocks = fields.get("blocks")
    if fields.get("algorithm") != "SHA256" or not isinstance(blocks, list):
        raise ValueError("invalid ASAR integrity information")
    block_size = require_size(fields.get("blockSize"))
    if block_size == 0:
        raise ValueError("invalid ASAR integrity block size")
    return Integrity(require_digest(fields.get("hash")), block_size, tuple(map(require_digest, blocks)))


def parse_entry(value: object) -> Entry:
    fields = require_object(value)
    if set(fields) == {"files"}:
        children = require_object(fields["files"])
        return Directory(tuple((name, parse_entry(child)) for name, child in children.items()))
    size = require_size(fields.get("size"))
    integrity = parse_integrity(fields.get("integrity"))
    if set(fields) == {"size", "integrity", "unpacked"} and fields["unpacked"] is True:
        return Unpacked(size, integrity)
    if not {"size", "integrity", "offset"} <= fields.keys() <= {"size", "integrity", "offset", "executable"}:
        raise ValueError("unsupported ASAR file entry")
    offset = fields["offset"]
    executable = fields.get("executable")
    if not isinstance(offset, str) or not offset.isascii() or not offset.isdecimal():
        raise ValueError("invalid ASAR file offset")
    if executable is not None and not isinstance(executable, bool):
        raise ValueError("invalid ASAR executable marker")
    return Packed(int(offset), size, integrity, executable)


def read_header(file: BinaryIO) -> Header:
    file.seek(0)
    first, total, payload, length = struct.unpack("<4I", read_exact(file, 16))
    padding = (-length) % 4
    if first != 4 or length > HEADER_LIMIT or payload != length + padding + 4 or total != payload + 4:
        raise ValueError("unexpected ASAR pickle header")
    root = parse_entry(json.loads(read_exact(file, length)))
    if not isinstance(root, Directory) or read_exact(file, padding) != bytes(padding):
        raise ValueError("unexpected ASAR header")
    return Header(8 + total, root)


def encode_entry(entry: Entry) -> dict[str, object]:
    if isinstance(entry, Directory):
        return {"files": {name: encode_entry(child) for name, child in entry.children}}
    integrity = entry.integrity
    fields: dict[str, object] = {
        "size": entry.size,
        "integrity": {
            "algorithm": "SHA256",
            "hash": integrity.digest,
            "blockSize": integrity.block_size,
            "blocks": integrity.blocks,
        },
    }
    if isinstance(entry, Unpacked):
        fields["unpacked"] = True
    else:
        fields["offset"] = str(entry.offset)
        if entry.executable is not None:
            fields["executable"] = entry.executable
    return fields


def encode_header(root: Directory) -> bytes:
    data = json.dumps(encode_entry(root), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padding = (-len(data)) % 4
    payload = len(data) + padding + 4
    return struct.pack("<4I", 4, payload + 4, payload, len(data)) + data + bytes(padding)


def integrity_of(data: bytes, block_size: int) -> Integrity:
    blocks = tuple(hashlib.sha256(data[start:start + block_size]).hexdigest()
                   for start in range(0, len(data), block_size))
    return Integrity(hashlib.sha256(data).hexdigest(), block_size, blocks)


def packed_entry(root: Directory, path: tuple[str, ...]) -> Packed:
    entry: Entry = root
    for name in path:
        if not isinstance(entry, Directory):
            raise ValueError(f"ASAR path is not a directory: {name}")
        for child_name, child in entry.children:
            if child_name == name:
                entry = child
                break
        else:
            raise ValueError(f"ASAR path not found: {'/'.join(path)}")
    if not isinstance(entry, Packed):
        raise ValueError(f"expected a packed ASAR file: {'/'.join(path)}")
    return entry


def replace_file(
    root: Directory, target_path: tuple[str, ...], target: Packed, new_content: bytes,
    old_body_size: int, added_name: str, added_content: bytes,
) -> Directory:
    new_size = len(new_content)
    delta = new_size - target.size
    old_end = target.offset + target.size
    block_size = target.integrity.block_size
    added = Packed(old_body_size + delta, len(added_content), integrity_of(added_content, block_size), None)

    def visit(directory: Directory, path: tuple[str, ...]) -> Directory:
        children: list[tuple[str, Entry]] = []
        for name, entry in directory.children:
            child_path = (*path, name)
            if isinstance(entry, Directory):
                entry = visit(entry, child_path)
            elif isinstance(entry, Packed):
                end = entry.offset + entry.size
                if end > old_body_size:
                    raise ValueError("invalid packed ASAR entry location")
                if child_path == target_path:
                    entry = replace(entry, size=new_size, integrity=integrity_of(new_content, block_size))
                elif entry.size and entry.offset < old_end and end > target.offset:
                    raise ValueError("patch target overlaps or aliases a second ASAR entry")
                elif entry.offset >= old_end:
                    entry = replace(entry, offset=entry.offset + delta)
            children.append((name, entry))
        if path == target_path[:-1]:
            if any(name == added_name for name, _ in children):
                raise ValueError("reconnect module already exists in source archive")
            children.append((added_name, added))
        return Directory(tuple(children))

    return visit(root, ())


def blocks_from(file: BinaryIO, offset: int, length: int) -> Iterator[bytes]:
    file.seek(offset)
    while length:
        data = read_exact(file, min(length, COPY_BLOCK))
        yield data
        length -= len(data)

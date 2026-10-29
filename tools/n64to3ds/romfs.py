"""RomFS creation and verification helpers.

devkitPro's ``mkromfs3ds`` emits the *level 3* RomFS payload (the metadata and
file data), not the IVFC container that ``makerom -romfs`` expects.  The 3DS
IVFC layout used here is the one produced by ``3dsxtool``/``3dstool``:

    IVFC header (0x5c, padded to 0x60)
    level 0 (master) SHA-256 hashes, padded to 0x1000
    level 3 data (mkromfs3ds output), padded to 0x1000
    level 1 hashes (hashes of level 2), padded to 0x1000
    level 2 hashes (hashes of level 3), padded to 0x1000

The logical offsets stored in the IVFC header are not file offsets; they are
the offsets consumed by the 3DS RomFS hash verifier.  ``3dstool`` uses the same
scheme (``SRomFsLevel::LogicOffset``), so this container is accepted by
makerom and is extractable by 3dstool.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .errors import BuildError, VerificationError

IVFC_MAGIC = b"IVFC"
IVFC_VERSION = 0x00010000
IVFC_HEADER_SIZE = 0x5C
IVFC_HEADER_PADDED = 0x60
IVFC_BLOCK_SIZE = 0x1000
IVFC_HASH_SIZE = 0x20
ROMFS_META_HEADER_SIZE = 0x28


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _sha256_blocks(data: bytes, block_size: int = IVFC_BLOCK_SIZE) -> bytes:
    if len(data) % block_size:
        data += b"\x00" * (block_size - len(data) % block_size)
    return b"".join(
        hashlib.sha256(data[index : index + block_size]).digest()
        for index in range(0, len(data), block_size)
    )


def wrap_ivfc(raw_level3: bytes) -> bytes:
    """Wrap a level-3 RomFS payload in the standard IVFC container."""

    if not raw_level3:
        raise BuildError("mkromfs3ds produced an empty RomFS payload")

    level3 = raw_level3 + b"\x00" * ((-len(raw_level3)) % IVFC_BLOCK_SIZE)
    level2 = _sha256_blocks(level3)
    level1 = _sha256_blocks(level2)
    level0 = _sha256_blocks(level1)

    level2_offset = _align(len(level1), IVFC_BLOCK_SIZE)
    level3_offset = _align(level2_offset + len(level2), IVFC_BLOCK_SIZE)

    header = bytearray()
    header += struct.pack("<4sII", IVFC_MAGIC, IVFC_VERSION, len(level0))
    header += struct.pack("<QQII", 0, len(level1), 12, 0)
    header += struct.pack("<QQII", level2_offset, len(level2), 12, 0)
    header += struct.pack("<QQII", level3_offset, len(raw_level3), 12, 0)
    header += struct.pack("<II", IVFC_HEADER_SIZE, 0)
    if len(header) != IVFC_HEADER_SIZE:
        raise AssertionError("internal IVFC header size mismatch")

    output = bytearray()
    output += header
    output += b"\x00" * (IVFC_HEADER_PADDED - len(output))
    output += level0
    output += b"\x00" * (_align(len(output), IVFC_BLOCK_SIZE) - len(output))
    output += level3
    output += level1
    output += b"\x00" * (_align(len(output), IVFC_BLOCK_SIZE) - len(output))
    output += level2
    output += b"\x00" * (_align(len(output), IVFC_BLOCK_SIZE) - len(output))
    return bytes(output)


def build_romfs(
    source_dir: str | Path,
    raw_output: str | Path,
    ivfc_output: str | Path,
    *,
    mkromfs3ds: str = "mkromfs3ds",
    runner=None,
) -> tuple[Path, Path]:
    """Run mkromfs3ds and write both the level-3 payload and IVFC container."""

    source = Path(source_dir)
    raw_path = Path(raw_output)
    ivfc_path = Path(ivfc_output)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    ivfc_path.parent.mkdir(parents=True, exist_ok=True)

    if runner is None:
        import subprocess

        def runner(command: list[str]) -> None:
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode != 0:
                detail = (result.stdout + result.stderr).strip()
                raise BuildError(
                    f"mkromfs3ds failed with exit code {result.returncode}.",
                    hint=detail or "Run with --verbose to see the tool output.",
                )

    runner([mkromfs3ds, str(source), str(raw_path)])
    if not raw_path.is_file() or raw_path.stat().st_size == 0:
        raise BuildError(f"mkromfs3ds did not create {raw_path}.")

    wrapped = wrap_ivfc(raw_path.read_bytes())
    ivfc_path.write_bytes(wrapped)
    return raw_path, ivfc_path


@dataclass(frozen=True)
class RomFsMeta:
    size: int
    dir_hash_offset: int
    dir_hash_size: int
    dir_offset: int
    dir_size: int
    file_hash_offset: int
    file_hash_size: int
    file_offset: int
    file_size: int
    data_offset: int


@dataclass(frozen=True)
class RomFsFile:
    path: str
    offset: int
    size: int
    data: bytes


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise VerificationError("RomFS metadata is truncated.")
    return struct.unpack_from("<I", data, offset)[0]


def parse_meta(level3: bytes) -> RomFsMeta:
    if len(level3) < ROMFS_META_HEADER_SIZE:
        raise VerificationError("RomFS level-3 data is too small for its metadata header.")
    values = struct.unpack_from("<10I", level3, 0)
    meta = RomFsMeta(
        size=values[0],
        dir_hash_offset=values[1],
        dir_hash_size=values[2],
        dir_offset=values[3],
        dir_size=values[4],
        file_hash_offset=values[5],
        file_hash_size=values[6],
        file_offset=values[7],
        file_size=values[8],
        data_offset=values[9],
    )
    if meta.size != ROMFS_META_HEADER_SIZE:
        raise VerificationError(
            f"Unexpected RomFS metadata header size 0x{meta.size:X}; expected 0x28."
        )
    if meta.data_offset > len(level3):
        raise VerificationError("RomFS data offset is outside the level-3 payload.")
    return meta


def _read_name(level3: bytes, offset: int, name_size: int) -> str:
    if name_size in (0, 0xFFFFFFFF):
        return ""
    if name_size % 2:
        raise VerificationError("RomFS entry name has an odd byte length.")
    end = offset + name_size
    if end > len(level3):
        raise VerificationError("RomFS entry name is outside the level-3 payload.")
    try:
        return level3[offset:end].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise VerificationError(f"RomFS entry name is not valid UTF-16: {exc}") from exc


def iter_files(level3: bytes) -> Iterator[RomFsFile]:
    """Walk the RomFS directory/file metadata and yield every file.

    The traversal mirrors 3dstool's reader: directory and file offsets are
    relative to the start of their metadata sections, sibling offsets are
    relative to the same section, and file data offsets are relative to
    ``data_offset``.
    """

    meta = parse_meta(level3)
    visited_dirs: set[int] = set()
    visited_files: set[int] = set()

    def walk_files(offset: int, prefix: str) -> Iterator[RomFsFile]:
        while offset != 0xFFFFFFFF:
            if offset in visited_files:
                raise VerificationError("RomFS file metadata contains a cycle.")
            visited_files.add(offset)
            base = meta.file_offset + offset
            parent, sibling, data_offset, data_size, _prev, name_size = struct.unpack_from(
                "<IIQQII", level3, base
            )
            del parent
            name = _read_name(level3, base + 0x20, name_size)
            if name:
                path = prefix + name
                absolute = meta.data_offset + data_offset
                end = absolute + data_size
                if absolute < meta.data_offset or end > len(level3):
                    raise VerificationError(f"RomFS file {path!r} is outside the payload.")
                yield RomFsFile(path=path, offset=absolute, size=data_size, data=level3[absolute:end])
            offset = sibling

    def walk_dirs(offset: int, prefix: str) -> Iterator[RomFsFile]:
        while offset != 0xFFFFFFFF:
            if offset in visited_dirs:
                raise VerificationError("RomFS directory metadata contains a cycle.")
            visited_dirs.add(offset)
            base = meta.dir_offset + offset
            parent, sibling, child_dir, child_file, _prev, name_size = struct.unpack_from(
                "<IIIIII", level3, base
            )
            del parent
            name = _read_name(level3, base + 0x18, name_size)
            child_prefix = prefix + name + "/" if name else prefix
            yield from walk_files(child_file, child_prefix)
            yield from walk_dirs(child_dir, child_prefix)
            offset = sibling

    yield from walk_dirs(0, "")


def extract_file(level3: bytes, path: str) -> RomFsFile:
    wanted = path.strip("/")
    for entry in iter_files(level3):
        if entry.path == wanted:
            return entry
    raise VerificationError(f"RomFS does not contain {path!r}.")


@dataclass(frozen=True)
class IvfcInfo:
    master_hash_size: int
    level1_size: int
    level2_size: int
    level3_size: int
    level1_offset: int
    level2_offset: int
    level3_offset: int
    level3_physical: int


def parse_ivfc(data: bytes, start: int = 0) -> IvfcInfo:
    if start < 0 or start + IVFC_HEADER_SIZE > len(data):
        raise VerificationError("IVFC header is outside the image.")
    magic, version, master_hash_size = struct.unpack_from("<4sII", data, start)
    if magic != IVFC_MAGIC:
        raise VerificationError(f"Missing IVFC magic at 0x{start:X} (found {magic!r}).")
    if version != IVFC_VERSION:
        raise VerificationError(f"Unsupported IVFC version 0x{version:X}.")
    levels = []
    for index in range(3):
        offset, size, block_size, reserved = struct.unpack_from(
            "<QQII", data, start + 0x0C + index * 0x18
        )
        if block_size != 12:
            raise VerificationError(f"IVFC level {index + 1} block size is not 4 KiB.")
        if reserved != 0:
            raise VerificationError(f"IVFC level {index + 1} reserved field is non-zero.")
        levels.append((offset, size))
    header_size, reserved = struct.unpack_from("<II", data, start + 0x54)
    if header_size != IVFC_HEADER_SIZE:
        raise VerificationError(f"IVFC header size field is 0x{header_size:X}, expected 0x5C.")
    if reserved != 0:
        raise VerificationError("IVFC trailing reserved field is non-zero.")

    level3_physical = _align(IVFC_HEADER_PADDED + master_hash_size, IVFC_BLOCK_SIZE)
    level1_physical = _align(level3_physical + _align(levels[2][1], IVFC_BLOCK_SIZE), IVFC_BLOCK_SIZE)
    level2_physical = _align(level1_physical + _align(levels[0][1], IVFC_BLOCK_SIZE), IVFC_BLOCK_SIZE)

    info = IvfcInfo(
        master_hash_size=master_hash_size,
        level1_size=levels[0][1],
        level2_size=levels[1][1],
        level3_size=levels[2][1],
        level1_offset=levels[0][0],
        level2_offset=levels[1][0],
        level3_offset=levels[2][0],
        level3_physical=level3_physical,
    )
    if start + level2_physical + info.level2_size > len(data):
        raise VerificationError("IVFC level data extends beyond the image.")
    return info


def verify_ivfc(data: bytes, start: int = 0) -> IvfcInfo:
    info = parse_ivfc(data, start)
    # Offsets inside the IVFC container are relative to its own start.  A
    # RomFS partition is only 0x200-aligned inside the NCCH, so aligning
    # absolute file offsets here would shift the hash levels by the remainder
    # of the partition offset.
    level3_relative = info.level3_physical
    level1_relative = _align(
        level3_relative + _align(info.level3_size, IVFC_BLOCK_SIZE), IVFC_BLOCK_SIZE
    )
    level2_relative = _align(
        level1_relative + _align(info.level1_size, IVFC_BLOCK_SIZE), IVFC_BLOCK_SIZE
    )
    level3_physical = start + level3_relative
    level1_physical = start + level1_relative
    level2_physical = start + level2_relative

    level3 = data[level3_physical : level3_physical + info.level3_size]
    if len(level3) != info.level3_size:
        raise VerificationError("IVFC level 3 data is truncated.")
    expected_level2 = data[level2_physical : level2_physical + info.level2_size]
    computed_level2 = _sha256_blocks(level3)
    if expected_level2 != computed_level2:
        raise VerificationError("IVFC level 2 hashes do not match the level 3 data.")
    expected_level1 = data[level1_physical : level1_physical + info.level1_size]
    computed_level1 = _sha256_blocks(expected_level2)
    if expected_level1 != computed_level1:
        raise VerificationError("IVFC level 1 hashes do not match the level 2 data.")
    expected_master = data[start + IVFC_HEADER_PADDED : start + IVFC_HEADER_PADDED + info.master_hash_size]
    computed_master = _sha256_blocks(expected_level1)
    if expected_master != computed_master:
        raise VerificationError("IVFC master hash does not match the level 1 data.")
    return info


def level3_data(data: bytes, start: int = 0) -> bytes:
    info = parse_ivfc(data, start)
    begin = start + info.level3_physical
    end = begin + info.level3_size
    if end > len(data):
        raise VerificationError("IVFC level 3 data is truncated.")
    return data[begin:end]

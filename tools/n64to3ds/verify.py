"""CIA/NCCH/RomFS verification for n64to3ds output.

This is intentionally a *structural* verifier: it proves that the CIA contains
the expected application title id and an uncompressed RomFS file whose bytes are
exactly the normalized input ROM.  It cannot prove that a 3DS boots the image
or that the game is playable; those statuses are tracked separately.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import VerificationError
from .rom import NormalizedRom
from .romfs import IvfcInfo, extract_file, iter_files, level3_data, verify_ivfc


CIA_HEADER_SIZE = 0x2020
NCCH_MAGIC = b"NCCH"
NCCH_MAGIC_OFFSET = 0x100
NCCH_PROGRAM_ID_OFFSET = 0x118
NCCH_PRODUCT_CODE_OFFSET = 0x150
NCCH_ROMFS_OFFSET = 0x1B0
NCCH_ROMFS_SIZE = 0x1B4
MEDIA_UNIT = 0x200


@dataclass
class CiaInfo:
    path: Path
    file_size: int
    content_offset: int
    content_size: int
    title_id: int
    product_code: str
    romfs_offset: int
    romfs_size: int
    ivfc: IvfcInfo
    level3_size: int
    files: list[tuple[str, int]]
    rom_file_path: str | None
    rom_size: int
    rom_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "file_size": self.file_size,
            "content_offset": f"0x{self.content_offset:X}",
            "content_size": self.content_size,
            "title_id": f"{self.title_id:016X}",
            "product_code": self.product_code,
            "romfs_offset": f"0x{self.romfs_offset:X}",
            "romfs_size": self.romfs_size,
            "ivfc": {
                "master_hash_size": self.ivfc.master_hash_size,
                "level1_size": self.ivfc.level1_size,
                "level2_size": self.ivfc.level2_size,
                "level3_size": self.ivfc.level3_size,
                "level3_physical": f"0x{self.ivfc.level3_physical:X}",
            },
            "rom_file_path": self.rom_file_path,
            "rom_size": self.rom_size,
            "rom_sha256": self.rom_sha256,
            "files": [{"path": name, "size": size} for name, size in self.files],
        }


def _decode_product_code(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _find_ncch(data: bytes) -> int:
    """Return the NCCH base (signature start), not the magic offset."""

    search_from = CIA_HEADER_SIZE
    while True:
        magic = data.find(NCCH_MAGIC, search_from)
        if magic < 0:
            break
        base = magic - NCCH_MAGIC_OFFSET
        if base >= 0 and base + 0x200 <= len(data):
            program_id = struct.unpack_from("<Q", data, base + NCCH_PROGRAM_ID_OFFSET)[0]
            product = data[base + NCCH_PRODUCT_CODE_OFFSET : base + NCCH_PRODUCT_CODE_OFFSET + 0x10]
            if program_id != 0 and b"\x00" in product:
                return base
        search_from = magic + 1
    raise VerificationError(
        "CIA does not contain a recognizable NCCH partition.",
        hint="The makerom step may have failed or produced a malformed image.",
    )


def inspect_cia(path: str | Path) -> CiaInfo:
    source = Path(path)
    if not source.is_file():
        raise VerificationError(f"CIA not found: {source}")
    data = source.read_bytes()
    if len(data) < CIA_HEADER_SIZE:
        raise VerificationError(f"{source} is too small to be a CIA.")
    if data[:4] != b"\x20\x20\x00\x00":
        raise VerificationError(
            f"{source} does not start with a CIA header (found {data[:4].hex()})."
        )

    content_offset = _find_ncch(data)
    title_id = struct.unpack_from("<Q", data, content_offset + NCCH_PROGRAM_ID_OFFSET)[0]
    product_code = _decode_product_code(
        data[content_offset + NCCH_PRODUCT_CODE_OFFSET : content_offset + NCCH_PRODUCT_CODE_OFFSET + 0x10]
    )
    content_size_units = struct.unpack_from("<I", data, content_offset + 0x104)[0]
    content_size = content_size_units * MEDIA_UNIT
    if content_size == 0 or content_offset + content_size > len(data):
        raise VerificationError(
            f"NCCH content size {content_size} at offset 0x{content_offset:X} is outside the CIA."
        )

    romfs_units, romfs_size_units = struct.unpack_from(
        "<II", data, content_offset + NCCH_ROMFS_OFFSET
    )
    if romfs_units == 0 or romfs_size_units == 0:
        raise VerificationError("NCCH has no RomFS partition; the ROM was not embedded.")
    romfs_offset = content_offset + romfs_units * MEDIA_UNIT
    romfs_size = romfs_size_units * MEDIA_UNIT
    if romfs_offset + romfs_size > content_offset + content_size:
        raise VerificationError("RomFS partition extends beyond the NCCH content.")

    ivfc = verify_ivfc(data, romfs_offset)
    level3 = level3_data(data, romfs_offset)
    files = [(entry.path, entry.size) for entry in iter_files(level3)]

    rom_file_path = None
    rom_size = 0
    rom_sha256 = ""
    for name, _size in files:
        if not name.lower().endswith(".z64"):
            continue
        entry = extract_file(level3, name)
        digest = hashlib.sha256(entry.data).hexdigest()
        # The builder uses a title-id-derived filename; choose the first z64
        # entry and expose its hash.  verify_cia() checks it against the input.
        rom_file_path = name
        rom_size = entry.size
        rom_sha256 = digest
        break
    if rom_file_path is None:
        raise VerificationError("RomFS does not contain an embedded .z64 ROM.")

    return CiaInfo(
        path=source,
        file_size=len(data),
        content_offset=content_offset,
        content_size=content_size,
        title_id=title_id,
        product_code=product_code,
        romfs_offset=romfs_offset,
        romfs_size=romfs_size,
        ivfc=ivfc,
        level3_size=len(level3),
        files=files,
        rom_file_path=rom_file_path,
        rom_size=rom_size,
        rom_sha256=rom_sha256,
    )


@dataclass
class VerificationResult:
    ok: bool
    cia: CiaInfo
    expected_title_id: int
    expected_sha256: str
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "title_id_matches": self.cia.title_id == self.expected_title_id,
            "embedded_rom_sha256_matches": self.cia.rom_sha256 == self.expected_sha256,
            "embedded_rom_size": self.cia.rom_size,
            "cia": self.cia.as_dict(),
            "errors": list(self.errors),
        }


def verify_cia(
    path: str | Path,
    rom: NormalizedRom,
    expected_title_id: int,
    *,
    expected_rom_name: str | None = None,
) -> VerificationResult:
    cia = inspect_cia(path)
    errors: list[str] = []
    if cia.title_id != expected_title_id:
        errors.append(
            f"NCCH title id is {cia.title_id:016X}, expected {expected_title_id:016X}."
        )
    if cia.rom_sha256 != rom.sha256:
        errors.append(
            f"Embedded ROM SHA-256 is {cia.rom_sha256}, expected {rom.sha256}."
        )
    if cia.rom_size != rom.size:
        errors.append(f"Embedded ROM is {cia.rom_size} bytes, expected {rom.size}.")
    if expected_rom_name and cia.rom_file_path != expected_rom_name:
        errors.append(
            f"Embedded ROM path is {cia.rom_file_path!r}, expected {expected_rom_name!r}."
        )
    return VerificationResult(
        ok=not errors,
        cia=cia,
        expected_title_id=expected_title_id,
        expected_sha256=rom.sha256,
        errors=errors,
    )

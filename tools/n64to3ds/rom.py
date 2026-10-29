"""N64 ROM detection, normalization, header validation and identity.

The emulator receives a byte-order-normalized big-endian (z64) image.  This
module deliberately does *not* treat the two header CRC words as proof that a
ROM is complete or correct: they are reported and compared, but a mismatch is a
warning because patched ROMs and homebrew are common.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import RomError

try:  # Pinned optional dependency; CRC verification is best-effort without it.
    import ipl3checksum
except Exception:  # pragma: no cover - exercised only on incomplete installs
    ipl3checksum = None


Z64_MAGIC = b"\x80\x37\x12\x40"
V64_MAGIC = b"\x37\x80\x40\x12"
N64_MAGIC = b"\x40\x12\x37\x80"

MAX_ROM_SIZE = 64 * 1024 * 1024
MIN_ROM_SIZE = 0x1000


@dataclass(frozen=True)
class RomHeader:
    clock_rate: int
    entry_point: int
    release: int
    crc1: int
    crc2: int
    name: str
    country_code: str
    raw_name: bytes

    @property
    def country_name(self) -> str:
        return {
            "E": "USA",
            "J": "Japan",
            "P": "Europe",
            "D": "Germany",
            "F": "France",
            "I": "Italy",
            "S": "Spain",
            "X": "PAL",
            "Y": "PAL",
            "L": "PAL",
        }.get(self.country_code, "unknown")


@dataclass
class NormalizedRom:
    data: bytes
    source_format: str
    source_path: Path
    source_size: int
    header: RomHeader
    sha256: str
    crc_match: bool | None
    crc_calculated: tuple[int, int] | None
    cic: str | None
    warnings: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def display_name(self) -> str:
        return self.header.name or self.source_path.stem

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path),
            "source_format": self.source_format,
            "source_size": self.source_size,
            "normalized_size": self.size,
            "sha256": self.sha256,
            "header": {
                "clock_rate": self.header.clock_rate,
                "entry_point": f"0x{self.header.entry_point:08X}",
                "release": f"0x{self.header.release:08X}",
                "crc1": f"0x{self.header.crc1:08X}",
                "crc2": f"0x{self.header.crc2:08X}",
                "name": self.header.name,
                "country_code": self.header.country_code,
                "country_name": self.header.country_name,
            },
            "crc_match": self.crc_match,
            "crc_calculated": (
                [f"0x{self.crc_calculated[0]:08X}", f"0x{self.crc_calculated[1]:08X}"]
                if self.crc_calculated
                else None
            ),
            "cic": self.cic,
            "warnings": list(self.warnings),
        }


def _detect_format(data: bytes, path: Path) -> str:
    if len(data) < 4:
        raise RomError(
            f"{path} is too small to be an N64 ROM ({len(data)} bytes).",
            hint="Expected at least a 4-byte N64 header magic.",
        )
    magic = data[:4]
    if magic == Z64_MAGIC:
        return "z64"
    if magic == V64_MAGIC:
        return "v64"
    if magic == N64_MAGIC:
        return "n64"
    raise RomError(
        f"{path} does not look like an N64 ROM (magic {magic.hex()}).",
        hint=(
            "Supported byte orders are z64 (80 37 12 40), v64 (37 80 40 12) "
            "and n64 (40 12 37 80).  A .zip or a different console image is not "
            "accepted; extract the ROM first."
        ),
    )


def _to_z64(data: bytes, fmt: str) -> bytes:
    if fmt == "z64":
        return data
    if fmt == "v64":
        # 16-bit byte-swapped image: 37 80 40 12 -> 80 37 12 40
        if len(data) % 2:
            raise RomError("v64 ROM has an odd number of bytes.")
        return bytes(data[i ^ 1] for i in range(len(data)))
    if fmt == "n64":
        # 32-bit little-endian image: 40 12 37 80 -> 80 37 12 40
        if len(data) % 4:
            raise RomError("n64 ROM size is not a multiple of four bytes.")
        return bytes(data[i ^ 3] for i in range(len(data)))
    raise AssertionError(f"unknown format {fmt}")


def _parse_header(data: bytes, path: Path) -> RomHeader:
    if data[:4] != Z64_MAGIC:
        raise RomError(f"Internal error: {path} was not normalized to z64.")
    clock_rate, entry, release, crc1, crc2 = struct.unpack(">IIIII", data[4:24])
    raw_name = data[0x20:0x34]
    name = raw_name.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()
    country = data[0x3E:0x3F].decode("latin-1", errors="replace")
    return RomHeader(
        clock_rate=clock_rate,
        entry_point=entry,
        release=release,
        crc1=crc1,
        crc2=crc2,
        name=name,
        country_code=country,
        raw_name=raw_name,
    )


def _calculate_crc(data: bytes) -> tuple[tuple[int, int] | None, str | None]:
    if ipl3checksum is None or len(data) < 0x100000:
        return None, None
    try:
        cic = ipl3checksum.detectCIC(data)
        if cic is None:
            cic = ipl3checksum.CICKind.CIC_6102_7101
        value = ipl3checksum.calculateChecksum(data, cic)
        if value is None:
            return None, getattr(cic, "name", str(cic))
        return (int(value[0]) & 0xFFFFFFFF, int(value[1]) & 0xFFFFFFFF), getattr(cic, "name", str(cic))
    except Exception:
        return None, None


def load_rom(path: str | Path) -> NormalizedRom:
    """Read, detect, normalize and validate an N64 ROM."""

    source = Path(path)
    if not source.exists():
        raise RomError(f"ROM not found: {source}")
    if not source.is_file():
        raise RomError(f"ROM path is not a regular file: {source}")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise RomError(f"Cannot read ROM {source}: {exc}") from exc

    fmt = _detect_format(raw, source)
    data = _to_z64(raw, fmt)
    if len(data) < MIN_ROM_SIZE:
        raise RomError(
            f"{source} is too small ({len(data)} bytes after normalization).",
            hint="An N64 ROM must contain at least the 4 KiB header/boot area.",
        )
    if len(data) > MAX_ROM_SIZE:
        raise RomError(f"{source} is larger than the 64 MiB N64 ROM limit.")
    if len(data) % 4:
        raise RomError(f"{source} size ({len(data)} bytes) is not a multiple of 4.")

    header = _parse_header(data, source)
    warnings: list[str] = []

    if header.clock_rate == 0:
        warnings.append("Header clock rate is zero (homebrew or unusual ROM).")
    if header.entry_point % 4:
        raise RomError(
            f"Header entry point 0x{header.entry_point:08X} is not 4-byte aligned.",
            hint="The ROM header is corrupt or this is not an N64 ROM.",
        )
    if not (0x80000000 <= header.entry_point < 0x80800000):
        warnings.append(
            f"Header entry point 0x{header.entry_point:08X} is outside the "
            "normal RDRAM range 0x80000000-0x807FFFFF."
        )
    if not header.name:
        warnings.append("ROM header name is empty; the file name will be used for the app title.")

    crc_calc, cic = _calculate_crc(data)
    crc_match: bool | None = None
    if crc_calc is not None:
        crc_match = crc_calc == (header.crc1, header.crc2)
        if not crc_match:
            warnings.append(
                "Header CRC1/CRC2 do not match the computed IPL3 checksum; "
                "continuing because patched ROMs and homebrew are common."
            )
    elif len(data) < 0x100000:
        warnings.append("ROM is shorter than 1 MiB; IPL3 CRC verification skipped.")

    sha = hashlib.sha256(data).hexdigest()
    return NormalizedRom(
        data=data,
        source_format=fmt,
        source_path=source,
        source_size=len(raw),
        header=header,
        sha256=sha,
        crc_match=crc_match,
        crc_calculated=crc_calc,
        cic=cic,
        warnings=warnings,
    )


def write_normalized(rom: NormalizedRom, destination: str | Path) -> Path:
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(rom.data)
    tmp.replace(dest)
    return dest

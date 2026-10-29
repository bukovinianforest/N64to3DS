"""Daedalus ``roms.ini`` identity and save-type handling.

Daedalus' 3DS runtime keys ROM settings by the header CRC words read as
*little-endian* ``u32`` values from the z64 image.  This is measured, not
inferred: for Harvest Moon 64 (USA), header bytes ``98 DF 9D FC`` /
``66 06 C1 89`` give ``g_ROM.mRomID.CRC = {0xFC9DDF98, 0x89C10666}`` on the
device, and the upstream ``Data/roms.ini`` uses the same spelling.

An earlier version of this module assumed the opposite (big-endian) spelling,
so generated entries were never found by the runtime.  The big-endian
``header_key`` is still looked up as a fallback for third-party databases
written that way, but entries are always emitted under ``runtime_key``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .errors import BuildError
from .rom import NormalizedRom

_SECTION_RE = re.compile(r"^\s*\{([0-9A-Fa-f]{16}-[0-9A-Fa-f]{2})\}\s*$")
_PROPERTY_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$")

# Properties that describe how Daedalus should emulate/save the ROM.  Preview
# images are deliberately omitted: they are not embedded in the CIA and would
# only produce a missing-file lookup.
_COPY_PROPERTIES = (
    "SaveType",
    "ExpansionPakUsage",
    "PatchesEnabled",
    "SpeedSyncEnabled",
    "DynarecSupported",
    "DynarecLoopOptimisation",
    "DynarecDoublesOptimisation",
    "DoubleDisplayEnabled",
    "CleanSceneEnabled",
    "ClearDepthFrameBuffer",
    "AudioRateMatch",
    "VideoRateMatch",
    "FogEnabled",
    "MemoryAccessOptimisation",
    "CheatsEnabled",
)


@dataclass(frozen=True)
class RomIds:
    runtime_key: str
    header_key: str
    country: int


@dataclass
class RomDbResult:
    runtime_key: str
    header_key: str
    found: bool
    matched_key: str | None
    properties: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def save_type(self) -> str | None:
        return self.properties.get("SaveType")


def compute_rom_ids(rom: NormalizedRom) -> RomIds:
    data = rom.data
    if len(data) < 0x40:
        raise BuildError("ROM is too small to contain a complete header.")
    crc1_be = int.from_bytes(data[0x10:0x14], "big")
    crc2_be = int.from_bytes(data[0x14:0x18], "big")
    crc1_le = int.from_bytes(data[0x10:0x14], "little")
    crc2_le = int.from_bytes(data[0x14:0x18], "little")
    country = data[0x3E]
    return RomIds(
        runtime_key=f"{crc1_le:08x}{crc2_le:08x}-{country:02x}",
        header_key=f"{crc1_be:08x}{crc2_be:08x}-{country:02x}",
        country=country,
    )


def parse_roms_ini(text: str) -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        section = _SECTION_RE.match(line)
        if section:
            current = {}
            entries[section.group(1).lower()] = current
            continue
        if current is None:
            continue
        prop = _PROPERTY_RE.match(line)
        if prop:
            current[prop.group(1)] = prop.group(2)
    return entries


def _lookup(
    entries: dict[str, dict[str, str]], ids: RomIds
) -> tuple[str | None, dict[str, str]]:
    for key in (ids.runtime_key, ids.header_key):
        if key in entries:
            return key, entries[key]
    return None, {}


def load_rom_db(path: str | Path) -> dict[str, dict[str, str]]:
    source = Path(path)
    if not source.is_file():
        raise BuildError(f"ROM database not found: {source}")
    try:
        return parse_roms_ini(source.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        raise BuildError(f"Cannot read ROM database {source}: {exc}") from exc


def lookup_rom_settings(
    rom: NormalizedRom,
    database: dict[str, dict[str, str]],
    *,
    save_type_override: str | None = None,
) -> RomDbResult:
    ids = compute_rom_ids(rom)
    matched_key, properties = _lookup(database, ids)
    selected = {key: value for key, value in properties.items() if key in _COPY_PROPERTIES}
    warnings: list[str] = []
    if matched_key is None:
        warnings.append(
            "ROM was not found in the Daedalus roms.ini database; "
            "save type and expansion-pak settings are unknown. "
            "Daedalus may be unable to create a game save until an entry is supplied with --rom-db."
        )
    if save_type_override:
        selected["SaveType"] = save_type_override
    return RomDbResult(
        runtime_key=ids.runtime_key,
        header_key=ids.header_key,
        found=matched_key is not None,
        matched_key=matched_key,
        properties=selected,
        warnings=warnings,
    )


def _escape_ini(value: str) -> str:
    # CIniFile reads the rest of the line verbatim; strip characters that
    # would turn into a new section/property if a ROM name contains them.
    return value.replace("\r", " ").replace("\n", " ").strip()


def render_generated_entry(
    rom: NormalizedRom,
    result: RomDbResult,
    *,
    game_name: str | None = None,
) -> str:
    name = _escape_ini(game_name or result.properties.get("Name") or rom.display_name)
    lines = [
        f"{{{result.runtime_key.upper()}}}",
        f"Name={name}",
        "Comment=Generated by n64to3ds from the embedded ROM header",
    ]
    for key in _COPY_PROPERTIES:
        value = result.properties.get(key)
        if value:
            lines.append(f"{key}={_escape_ini(value)}")
    lines.append("")
    return "\n".join(lines)


def generate_roms_ini(
    rom: NormalizedRom,
    upstream_path: str | Path,
    destination: str | Path,
    *,
    database: dict[str, dict[str, str]] | None = None,
    save_type_override: str | None = None,
    game_name: str | None = None,
) -> RomDbResult:
    """Write a merged roms.ini containing upstream data plus this ROM's entry.

    The full upstream database is kept because it is small (under 100 KiB) and
    makes the CIA independent of any external Daedalus data file.  The
    generated entry is appended last, so Daedalus' ``SetSettings`` call
    replaces any earlier entry with the same id.
    """

    source = Path(upstream_path)
    if database is None:
        if source.is_file():
            database = load_rom_db(source)
        else:
            database = {}

    result = lookup_rom_settings(rom, database, save_type_override=save_type_override)

    if source.is_file():
        try:
            header = source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise BuildError(f"Cannot read ROM database {source}: {exc}") from exc
        if not header.endswith("\n"):
            header += "\n"
    else:
        header = (
            "//Rom Initialiser File for DaedalusX64\n"
            "//Generated by n64to3ds: upstream Data/roms.ini was not available.\n\n"
        )
        result.warnings.append(
            "Upstream Daedalus roms.ini was not available; the CIA contains only the generated entry."
        )

    generated = render_generated_entry(rom, result, game_name=game_name)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(header + "\n" + generated, encoding="utf-8")
    return result


def upstream_roms_ini_path(repo_root: Path) -> Path:
    return repo_root / "third_party" / "daedalus" / "Data" / "roms.ini"


def filter_properties(properties: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {key: value for key, value in properties if key in _COPY_PROPERTIES}

"""Stable, collision-checked CIA title IDs.

3DS application title IDs have the form ``00040000_0UUUUU00``.  The low 24
bits are a per-application unique id.  We derive those bits from the SHA-256 of
the *normalized* ROM and keep a small local registry so two different ROMs
cannot silently receive the same title id.  The registry is advisory for other
machines but deterministic for repeated builds on one machine.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from .errors import RomError

APPLICATION_BASE = 0x0004000000000000
APPLICATION_HIGH = 0x00040000
UNIQUE_MASK = 0xFFFFF  # makerom accepts 20-bit UniqueId values (0x00001-0xFFFFF)
TITLE_ID_RE = re.compile(r"^[0-9A-Fa-f]{16}$")
UNIQUE_RE = re.compile(r"^[0-9A-Fa-f]{1,5}$")


def format_title_id(title_id: int) -> str:
    return f"{title_id:016X}"


def format_unique_id(unique_id: int) -> str:
    return f"{unique_id:06X}"


def derive_unique_id(sha256_hex: str) -> int:
    """Domain-separated 24-bit id; stable for a given normalized ROM."""

    digest = hashlib.sha256(b"n64to3ds-title-v1\x00" + bytes.fromhex(sha256_hex)).digest()
    value = int.from_bytes(digest[:4], "big") & UNIQUE_MASK
    # Avoid the reserved-looking all-zero value.
    return value or 1


def title_id_from_unique(unique_id: int) -> int:
    return APPLICATION_BASE | ((unique_id & UNIQUE_MASK) << 8)


def parse_title_id(value: str) -> tuple[int, int]:
    """Return (full_title_id, unique_id) from an explicit user value.

    Accepted forms:
      * 16 hex digits: full title id (must be an application id)
      * 8 hex digits: low 32 bits, whose low byte must be zero
      * 1-5 hex digits: the 20-bit unique id
    """

    text = value.strip().replace("_", "").replace("-", "")
    if text.lower().startswith("0x"):
        text = text[2:]
    if TITLE_ID_RE.match(text):
        full = int(text, 16)
        if (full >> 32) != APPLICATION_HIGH:
            raise RomError(
                f"Title id {value} is not an application title id.",
                hint="n64to3ds only creates application CIAs with high word 00040000.",
            )
        if full & 0xFF:
            raise RomError(
                f"Title id {value} has a non-zero low byte; 3DS application ids end in 00.",
            )
        if (full >> 28) & 0xF:
            raise RomError(
                f"Title id {value} uses more than the 20-bit unique id supported by makerom.",
                hint="Use a title id of the form 0004000000UUUUU00 with U in 00001-FFFFF.",
            )
        unique = (full >> 8) & UNIQUE_MASK
        return full, unique
    if re.match(r"^[0-9A-Fa-f]{8}$", text):
        low = int(text, 16)
        if low & 0xFF:
            raise RomError(f"Low title-id word {value} must end in 00.")
        if (low >> 28) & 0xF:
            raise RomError(
                f"Low title-id word {value} uses more than the 20-bit unique id supported by makerom.",
                hint="Use a low word of the form 00UUUUU0 with U in 00001-FFFFF.",
            )
        unique = (low >> 8) & UNIQUE_MASK
        return title_id_from_unique(unique), unique
    if UNIQUE_RE.match(text):
        unique = int(text, 16) & UNIQUE_MASK
        if unique == 0:
            raise RomError("Unique id 0 is not allowed.")
        return title_id_from_unique(unique), unique
    raise RomError(
        f"Cannot parse title id {value!r}.",
        hint="Use 16 hex digits (0004000000ABCD00), 8 hex digits, or a 20-bit unique id.",
    )


def registry_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "n64to3ds" / "titles.json"
    return Path.home() / ".local" / "share" / "n64to3ds" / "titles.json"


def _load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "titles": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "titles": {}}
    if not isinstance(value, dict):
        return {"version": 1, "titles": {}}
    value.setdefault("version", 1)
    value.setdefault("titles", {})
    if not isinstance(value["titles"], dict):
        value["titles"] = {}
    return value


def _write_registry(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".titles-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def choose_title_id(
    sha256_hex: str,
    rom_name: str,
    explicit: str | None = None,
    *,
    use_registry: bool = True,
) -> tuple[int, int, list[str]]:
    """Choose a title id and update the local registry.

    Returns (full_title_id, unique_id, warnings).  Raises RomError if an
    explicit id is already registered to a different ROM.
    """

    warnings: list[str] = []
    path = registry_path()
    lock_handle = None
    registry = {"version": 1, "titles": {}}

    if use_registry:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_handle = open(path.with_suffix(".lock"), "a+")
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            registry = _load_registry(path)
        except OSError as exc:
            warnings.append(f"Title-id registry unavailable ({exc}); using the ROM-derived id without cross-build checking.")
            use_registry = False
            registry = {"version": 1, "titles": {}}

    try:
        titles: dict[str, Any] = registry.setdefault("titles", {})

        if explicit is not None:
            full, unique = parse_title_id(explicit)
            key = format_title_id(full)
            existing = titles.get(key)
            if existing and existing.get("sha256") not in (None, sha256_hex):
                raise RomError(
                    f"Title id {key} is already registered to a different ROM "
                    f"({existing.get('name', 'unknown')}, SHA-256 {existing.get('sha256', 'unknown')}).",
                    hint=(
                        f"Choose another --title-id or remove the entry from {path}. "
                        "Refusing to create a silent title-id collision."
                    ),
                )
            titles[key] = {
                "sha256": sha256_hex,
                "name": rom_name,
                "updated": int(time.time()),
            }
            return full, unique, warnings

        unique = derive_unique_id(sha256_hex)
        full = title_id_from_unique(unique)
        # Probe deterministically if this machine already used the id for a
        # different ROM.  This is the only collision source we can know about.
        for _ in range(4096):
            key = format_title_id(full)
            existing = titles.get(key)
            if existing is None or existing.get("sha256") in (None, sha256_hex):
                titles[key] = {
                    "sha256": sha256_hex,
                    "name": rom_name,
                    "updated": int(time.time()),
                }
                return full, unique, warnings
            unique = (unique + 1) & UNIQUE_MASK
            if unique == 0:
                unique = 1
            full = title_id_from_unique(unique)
        raise RomError(
            "Could not find a free derived title id in the local registry.",
            hint="Use --title-id to choose one explicitly.",
        )
    finally:
        if use_registry and lock_handle is not None:
            try:
                _write_registry(path, registry)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

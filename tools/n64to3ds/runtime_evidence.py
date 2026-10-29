"""Factual runtime-evidence recording.

A built CIA only proves that packaging succeeded.  This module parses the
logs written by the Daedalus 3DS runtime and updates the manifest's status
fields only for claims those logs can actually support:

* ``emulator boot verified`` requires a rendered first frame.
* ``AOT exercised`` requires at least one generated block to have executed
  against the exact ROM SHA-256 recorded in the manifest.

Playability and full speed are deliberately not inferred.  A first frame, a
heartbeat, or a non-zero block counter cannot prove either one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import N64To3DSError

FIRST_FRAME_RE = re.compile(
    r"n64to3ds first frame pc=0x(?P<pc>[0-9A-Fa-f]+)"
    r"(?:\s+title=(?P<title>[0-9A-Fa-f]+))?"
)
FIRST_BLOCK_RE = re.compile(
    r"first_block_executed=0x(?P<pc>[0-9A-Fa-f]+)\s+"
    r"rom=0x(?P<rom>[0-9A-Fa-f]+)\s+"
    r"instructions=(?P<instructions>\d+)"
)
STATS_RE = re.compile(r"^stats\[(?P<label>[^\]]+)\]:\s*(?P<body>.*)$")
FRAME_RE = re.compile(r"^frame:\s*(?P<body>.*)$")
HEARTBEAT_RE = re.compile(r"^heartbeat:\s*(?P<body>.*)$")
NEW_BLOCK_RE = re.compile(r"^new_block=0x(?P<pc>[0-9A-Fa-f]+)")
ROM_SHA_RE = re.compile(r"^rom_sha256=(?P<sha>[0-9A-Fa-f]{64})$")
MODE_RE = re.compile(r"^mode=(?P<mode>\S+)$")
GAME_RE = re.compile(r"^game=(?P<game>.*)$")
KEY_VALUE_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S+)")


class RuntimeEvidenceError(N64To3DSError):
    """The supplied runtime logs cannot support the requested claims."""

    exit_code = 1


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_numbers(body: str) -> dict[str, int | float | str]:
    values: dict[str, int | float | str] = {}
    for match in KEY_VALUE_RE.finditer(body):
        key = match.group("key")
        raw = match.group("value")
        try:
            if raw.startswith("0x") or raw.startswith("0X"):
                values[key] = int(raw, 16)
            elif re.fullmatch(r"-?\d+", raw):
                values[key] = int(raw)
            elif re.fullmatch(r"-?\d+\.\d+", raw):
                values[key] = float(raw)
            else:
                values[key] = raw
        except ValueError:
            values[key] = raw
    return values


@dataclass
class AotLogSummary:
    path: Path
    sha256: str
    mode: str | None = None
    rom_sha256: str | None = None
    game: str | None = None
    first_block: dict[str, int] | None = None
    stats: dict[str, int | float | str] = field(default_factory=dict)
    stats_label: str | None = None
    frame: dict[str, int | float | str] = field(default_factory=dict)
    heartbeat_count: int = 0
    new_block_count: int = 0
    lines: int = 0

    @property
    def block_executions(self) -> int:
        values = [
            self.stats.get("blocks", 0),
            self.frame.get("aot_hits", 0),
        ]
        numbers = [int(value) for value in values if isinstance(value, (int, float))]
        return max(numbers) if numbers else 0

    @property
    def instruction_executions(self) -> int:
        values = [
            self.stats.get("instructions", 0),
            self.frame.get("aot_instructions", 0),
        ]
        numbers = [int(value) for value in values if isinstance(value, (int, float))]
        return max(numbers) if numbers else 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "mode": self.mode,
            "rom_sha256": self.rom_sha256,
            "game": self.game,
            "first_block": self.first_block,
            "latest_stats_label": self.stats_label,
            "stats": self.stats,
            "block_executions": self.block_executions,
            "instruction_executions": self.instruction_executions,
            "frame": self.frame,
            "heartbeats": self.heartbeat_count,
            "new_blocks": self.new_block_count,
            "lines": self.lines,
        }


@dataclass
class DaedalusLogSummary:
    path: Path
    sha256: str
    first_frame_pc: int | None = None
    title_id: str | None = None
    cpu_halt_lines: list[str] = field(default_factory=list)
    lines: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "first_frame_pc": (
                f"0x{self.first_frame_pc:08X}" if self.first_frame_pc is not None else None
            ),
            "title_id": self.title_id,
            "cpu_halt_lines": self.cpu_halt_lines,
            "lines": self.lines,
        }


def parse_aot_log(path: str | Path) -> AotLogSummary:
    source = Path(path)
    if not source.is_file():
        raise RuntimeEvidenceError(f"AOT log not found: {source}")
    summary = AotLogSummary(path=source, sha256=_sha256_file(source))
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        summary.lines += 1
        stripped = line.strip()
        match = ROM_SHA_RE.match(stripped)
        if match:
            summary.rom_sha256 = match.group("sha").lower()
            continue
        match = MODE_RE.match(stripped)
        if match:
            summary.mode = match.group("mode")
            continue
        match = GAME_RE.match(stripped)
        if match:
            summary.game = match.group("game")
            continue
        match = FIRST_BLOCK_RE.search(stripped)
        if match:
            summary.first_block = {
                "pc": int(match.group("pc"), 16),
                "rom_offset": int(match.group("rom"), 16),
                "instructions": int(match.group("instructions")),
            }
            continue
        match = STATS_RE.match(stripped)
        if match:
            summary.stats_label = match.group("label")
            summary.stats = _parse_numbers(match.group("body"))
            continue
        match = FRAME_RE.match(stripped)
        if match:
            summary.frame = _parse_numbers(match.group("body"))
            continue
        if HEARTBEAT_RE.match(stripped):
            summary.heartbeat_count += 1
            continue
        if NEW_BLOCK_RE.match(stripped):
            summary.new_block_count += 1
    return summary


def parse_daedalus_log(path: str | Path) -> DaedalusLogSummary:
    source = Path(path)
    if not source.is_file():
        raise RuntimeEvidenceError(f"Daedalus log not found: {source}")
    summary = DaedalusLogSummary(path=source, sha256=_sha256_file(source))
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        summary.lines += 1
        match = FIRST_FRAME_RE.search(line)
        if match:
            summary.first_frame_pc = int(match.group("pc"), 16)
            if match.group("title"):
                summary.title_id = match.group("title").upper()
        if "CPU Halting:" in line:
            summary.cpu_halt_lines.append(line.strip())
    return summary


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _copy_evidence(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return destination


def _manifest_base(manifest_path: Path) -> str:
    name = manifest_path.name
    suffix = ".manifest.json"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    return manifest_path.stem


@dataclass
class RuntimeRecordResult:
    manifest_path: Path
    boot_verified: bool
    aot_exercised: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def record_runtime(
    manifest_path: str | Path,
    *,
    aot_log: str | Path | None = None,
    daedalus_log: str | Path | None = None,
    copy_logs: bool = True,
) -> RuntimeRecordResult:
    """Update a manifest with evidence parsed from runtime logs.

    The AOT log is cryptographically tied to the ROM in the manifest through
    the ``rom_sha256`` line written by the runtime.  The Daedalus log is tied
    to the image through the title id printed on the first rendered frame
    (new builds) or accepted as the matching run when the title field is
    absent (older diagnostic builds).
    """

    path = Path(manifest_path)
    if not path.is_file():
        raise RuntimeEvidenceError(f"Manifest not found: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeEvidenceError(f"Could not read manifest {path}: {exc}") from exc

    rom_sha = str(manifest.get("rom", {}).get("sha256", "")).lower()
    if not rom_sha:
        raise RuntimeEvidenceError("Manifest has no ROM SHA-256; cannot tie evidence to a ROM.")
    title_id = str(manifest.get("title", {}).get("title_id", "")).upper()
    aot_mode = str(manifest.get("aot", {}).get("mode", "off"))

    result = RuntimeRecordResult(
        manifest_path=path,
        boot_verified=False,
        aot_exercised=False,
    )
    evidence: dict[str, Any] = {
        "recorded_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rom_sha256": rom_sha,
        "aot_mode": aot_mode,
    }
    base = _manifest_base(path)

    if daedalus_log is not None:
        summary = parse_daedalus_log(daedalus_log)
        evidence["daedalus_log"] = summary.as_dict()
        if summary.first_frame_pc is not None:
            result.boot_verified = True
            evidence["first_frame_pc"] = f"0x{summary.first_frame_pc:08X}"
        if summary.title_id and title_id and summary.title_id != title_id:
            result.errors.append(
                f"Daedalus log title id {summary.title_id} does not match manifest title id {title_id}."
            )
        if summary.cpu_halt_lines:
            result.warnings.append(
                "Daedalus log contains a CPU halt: " + summary.cpu_halt_lines[-1]
            )
        if copy_logs:
            destination = path.with_name(base + ".runtime-daedalus.log")
            _copy_evidence(summary.path, destination)
            evidence["daedalus_log"]["copied_path"] = str(destination)
            evidence["daedalus_log"]["copied_sha256"] = _sha256_file(destination)

    if aot_log is not None:
        summary = parse_aot_log(aot_log)
        evidence["aot_log"] = summary.as_dict()
        if summary.rom_sha256 and summary.rom_sha256 != rom_sha:
            result.errors.append(
                "AOT log ROM SHA-256 does not match the manifest: "
                f"{summary.rom_sha256} != {rom_sha}."
            )
        if summary.mode and summary.mode != aot_mode:
            result.errors.append(
                f"AOT log mode {summary.mode!r} does not match manifest mode {aot_mode!r}."
            )
        if summary.first_block is not None and summary.block_executions > 0:
            result.aot_exercised = True
        elif aot_mode == "auto":
            result.warnings.append(
                "AOT log has no executed generated block; AOT exercised remains no."
            )
        if summary.frame:
            result.boot_verified = True
            evidence["first_frame_pc"] = evidence.get("first_frame_pc") or summary.frame.get(
                "last_pc"
            )
        if copy_logs:
            destination = path.with_name(base + ".runtime-aot.log")
            _copy_evidence(summary.path, destination)
            evidence["aot_log"]["copied_path"] = str(destination)
            evidence["aot_log"]["copied_sha256"] = _sha256_file(destination)

    if daedalus_log is None and aot_log is None:
        result.errors.append("No runtime logs were supplied.")

    if not result.boot_verified and not result.errors:
        result.errors.append(
            "No rendered first frame was found in the supplied logs; "
            "emulator boot verified remains no."
        )

    evidence["emulator_boot_verified"] = result.boot_verified
    evidence["aot_exercised"] = result.aot_exercised
    evidence["warnings"] = result.warnings
    evidence["errors"] = result.errors

    status = manifest.setdefault("status", {})
    status["CIA built"] = bool(status.get("CIA built", True))
    status["emulator boot verified"] = result.boot_verified
    status["AOT exercised"] = result.aot_exercised
    # These cannot be proven by these logs.  Preserve any explicit prior value
    # only if a future hardware workflow set it deliberately.
    status["game playable"] = bool(status.get("game playable", False))
    status["full speed"] = bool(status.get("full speed", False))
    status["hardware verified"] = bool(status.get("hardware verified", False))
    status["hardware-unverified"] = not bool(status["hardware verified"])
    manifest["runtime_evidence"] = evidence

    if result.ok:
        _atomic_write(path, json.dumps(manifest, indent=2) + "\n")
    result.evidence = evidence
    return result

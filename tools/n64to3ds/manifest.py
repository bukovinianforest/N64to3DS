"""Build manifest and status reporting."""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__


@dataclass
class Status:
    """Factual status of each acceptance milestone.

    These are deliberately separate.  A packaged CIA does not imply that the
    emulator booted, that AOT code ran on a 3DS, that the game is playable, or
    that it runs at full speed.
    """

    cia_built: bool = False
    emulator_boot_verified: bool = False
    aot_exercised: bool = False
    game_playable: bool = False
    full_speed: bool = False
    hardware_verified: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "CIA built": self.cia_built,
            "emulator boot verified": self.emulator_boot_verified,
            "AOT exercised": self.aot_exercised,
            "game playable": self.game_playable,
            "full speed": self.full_speed,
            "hardware verified": self.hardware_verified,
            "hardware-unverified": not self.hardware_verified,
        }


@dataclass
class BuildManifest:
    schema: int = 1
    tool_version: str = __version__
    created_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    host: dict[str, Any] = field(default_factory=dict)
    rom: dict[str, Any] = field(default_factory=dict)
    title: dict[str, Any] = field(default_factory=dict)
    aot: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)
    toolchain: dict[str, Any] = field(default_factory=dict)
    upstream: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    runtime_evidence: dict[str, Any] = field(default_factory=dict)
    status: Status = field(default_factory=Status)
    warnings: list[str] = field(default_factory=list)
    log_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.as_dict()
        return value

    def write(self, destination: str | Path) -> Path:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2) + "\n", encoding="utf-8")
        return path

    def print_status(self, stream=None) -> None:
        out = stream or sys.stdout
        labels = [
            ("CIA built", self.status.cia_built),
            ("emulator boot verified", self.status.emulator_boot_verified),
            ("AOT exercised", self.status.aot_exercised),
            ("game playable", self.status.game_playable),
            ("full speed", self.status.full_speed),
            ("hardware verified", self.status.hardware_verified),
        ]
        for label, value in labels:
            out.write(f"{label}: {'yes' if value else 'no'}\n")
        if not self.status.hardware_verified:
            out.write("hardware-unverified\n")


def host_info() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
    }

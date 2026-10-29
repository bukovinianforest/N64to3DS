"""Regenerate ``harvest_moon_64.us.json`` from a local hm64-decomp checkout.

This is a maintainer tool, not part of the build.  It only extracts *facts*
from the decompilation (symbol addresses, struct offsets, enum names and map
dimensions) so the runtime adapter never hard-codes or guesses them.  The
decompilation's baserom SHA-1 must match the ROM the profile is for.

Usage:
    python3 -m tools.n64to3ds.profiles.generate_hm64_profile \\
        --decomp /path/to/hm64-decomp --syms /path/to/hm64.us.datasyms.toml \\
        --rom /path/to/hm64.us.z64
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
from pathlib import Path

PROFILE_PATH = Path(__file__).with_name("harvest_moon_64.us.json")

# Symbols the adapter reads.  Names are the decomp's names; types come from
# the extern declarations noted next to each entry.
SYMBOLS = {
    "gPlayer": "Player (src/game/player.h)",
    "gGold": "u32 (src/game/game.h)",
    "gSeason": "u8 (src/game/time.h)",
    "gDayOfMonth": "u8 (src/game/time.h)",
    "gDayOfWeek": "u8 (src/game/time.h)",
    "gYear": "u8 (src/game/time.h)",
    "gHour": "u8 (src/game/time.h)",
    "gMinutes": "u8 (src/game/time.h)",
    "gMaximumStamina": "u8 (src/game/player.h)",
    "gBaseMapIndex": "u8 (src/game/level.h)",
    "gMapWithSeasonIndex": "u8 (src/game/level.h)",
    # Only used to decide when the bottom screen may swap inventory slots.
    "mainLoopCallbackCurrentIndex": "volatile u16 (src/mainLoop.h)",
    "toolUse": "ToolUse (src/game/items.h)",
    "entities": "Entity[MAX_ENTITIES] (src/system/entity.h)",
}

# Offsets inside ``Player`` (src/game/player.h, struct at 0x80189060).  They
# are recomputed from the struct field list by ``_player_layout`` and checked
# against the offset comments in the header.
PLAYER_FIELDS = (
    ("currentStamina", "u8", 1),
    ("name", "u8", 6),
    ("toolHeldCounter", "u16", 1),
    ("toolUseCounters", "u16", 5),
    ("currentToolLevel", "u8", 1),
    ("toolSlots", "u8", 8),
    ("currentTool", "u8", 1),
    ("bottleContents", "u8", 1),
    ("toolLevels", "u8", 5),
    ("belongingsSlots", "u8", 8),
    ("heldItem", "u8", 1),
    ("itemInfoIndex", "u8", 1),
    ("keyItemSlots", "u8", 24),
    ("unk_46", "u8", 1),
    ("unk_47", "u8", 1),
    ("coordinates", "f32", 3),
    ("movementVector", "f32", 3),
    ("velocity", "f32", 1),
    ("actionHandler", "u16", 1),
    ("animationHandler", "u16", 1),
    ("actionPhaseFrameCounter", "u16", 1),
    ("actionPhase", "u8", 1),
    ("shopItemIndex", "u8", 1),
    ("heldAnimalIndex", "u8", 1),
    ("direction", "u8", 1),
    ("savedDirection", "u8", 1),
    ("actionTimer", "u8", 1),
    ("groundObjectIndex", "u8", 1),
    ("fatigueCounter", "u8", 1),
    ("staminaExhaustionLevel", "u8", 1),
    ("fatigueThreshold", "u8", 1),
    ("fishingSpotType", "u8", 1),
    ("unused", "u8", 1),
    ("flags", "u16", 1),
)
PLAYER_EXPORTED = (
    "currentStamina", "toolSlots", "currentTool", "bottleContents", "toolLevels",
    "belongingsSlots", "heldItem", "keyItemSlots", "coordinates", "actionHandler", "direction", "flags",
)
_SIZES = {"u8": 1, "u16": 2, "u32": 4, "f32": 4}
# Offsets the header documents in comments; a mismatch means the struct
# definition changed and the profile must not be regenerated blindly.
_DOCUMENTED_OFFSETS = {"toolHeldCounter": 0x8, "toolUseCounters": 0xA,
                       "currentToolLevel": 0x14, "toolSlots": 0x15,
                       "currentTool": 0x1D, "bottleContents": 0x1E,
                       "coordinates": 0x48, "actionHandler": 0x64,
                       "direction": 0x6D, "flags": 0x76}

# Pause-screen sprite assets the bottom screen draws (splat segment names in
# config/us/splat.us.yaml) and the frames of each that the pause screen shows
# (overlayScreens.c: loadPauseScreenSprites, loadClockSprites,
# setMoneySprites, initializePauseScreenBackground), as animation -> frame
# count.  Tools and items only contribute the animations the icon tables
# below reference.
SPRITE_ASSETS = {
    # 0: checkerboard (drawn at scale 2)
    "background": ("checkerboardBackgroundTexture", "checkerboardBackgroundAssetsIndex", None, {0: 1}),
    # 0: wooden clock, 1: season names (gSeason - 1), 2: day digits,
    # 3: weekdays (gDayOfWeek), 4: minute hand, 5: hour hand, 6: cow-skin clock
    "clock": ("timeUITexture", "timeUIAssetsIndex", None, {0: 1, 1: 4, 2: 10, 3: 7, 4: 1, 5: 1, 6: 1}),
    # 0: scroll with "G", 1: digits
    "money": ("moneyTexture", "moneyAssetsIndex", None, {0: 1, 1: 10}),
    # 0: tool/belongings grid, 1: key item grid, 2: selection frame,
    # 5: "Tool"/"Belongings" labels, 6: blinking frame around a picked slot
    # (updatePauseScreenSelectionHighlight)
    "inventory": ("inventoryScreenTexture", "inventoryScreenAssetsIndex", None, {0: 1, 1: 1, 2: 1, 5: 1, 6: 2}),
    "tools": ("toolsTexture", "toolsAssetsIndex", "toolsSpritesheetIndex", None),
    "items": ("holdableItemsTexture", "holdableItemsAssetsIndex", "holdableItemsSpritesheetIndex", None),
}
# The selected-tool and held-item frames use inventory animation 2 with
# palettes 3 and 2 (loadPauseScreenSprites, setSpritePaletteIndex).
PALETTE_VARIANTS = [["inventory", 2, 0, 3], ["inventory", 2, 0, 2]]
# getToolLevelForAnimation: tools 1..5 add gPlayer.toolLevels[tool - 1],
# EMPTY_BOTTLE (29) adds gPlayer.bottleContents (0..7 are assigned in src/).
TOOL_LEVEL_COUNTS = {1: 3, 2: 3, 3: 3, 4: 3, 5: 3, 29: 8}

# ToolUse (src/game/items.h, 0x80189828): u16, u8, u8, s16, s16, u8, s16,
# s16, then toolUseState.
TOOL_USE_STATE_OFFSET = 0xE

_DEFINE_RE = re.compile(r"^#define\s+([A-Z0-9_]+)\s+(\d+)\s*(?://.*)?$")


def _player_layout() -> dict[str, dict[str, object]]:
    layout: dict[str, dict[str, object]] = {}
    offset = 0
    for name, kind, count in PLAYER_FIELDS:
        size = _SIZES[kind]
        offset = (offset + size - 1) // size * size
        layout[name] = {"offset": offset, "type": kind, "count": count}
        offset += size * count
    for name, expected in _DOCUMENTED_OFFSETS.items():
        actual = layout[name]["offset"]
        if actual != expected:
            raise SystemExit(f"Player.{name}: computed 0x{actual:X}, header says 0x{expected:X}")
    return {name: layout[name] for name in PLAYER_EXPORTED}


def _pretty(identifier: str) -> str:
    for suffix in ("_HELD_ITEM",):
        if identifier.endswith(suffix):
            identifier = identifier[: -len(suffix)]
    return " ".join(part.capitalize() for part in identifier.split("_"))


def _section_defines(text: str, start_marker: str, end_marker: str) -> dict[int, str]:
    lines = text.splitlines()
    begin = next(i for i, line in enumerate(lines) if start_marker in line)
    names: dict[int, str] = {}
    for line in lines[begin + 1:]:
        if end_marker and end_marker in line:
            break
        match = _DEFINE_RE.match(line.strip())
        if match:
            names.setdefault(int(match.group(2)), _pretty(match.group(1)))
    return names


def _maps(text: str) -> tuple[dict[int, str], dict[str, dict[str, int]]]:
    """Map indices come from the section before "Map Dimensions"; later
    sections (spawn points, ...) reuse small numbers for other things."""
    names: dict[int, str] = {}
    dims: dict[str, dict[str, int]] = {}
    in_index_section = True
    for line in text.splitlines():
        if "Map Dimensions" in line:
            in_index_section = False
        if "Tile-to-World" in line:
            break
        match = _DEFINE_RE.match(line.strip())
        if not match:
            continue
        ident, value = match.group(1), int(match.group(2))
        if ident.startswith("MAP_") and ident.endswith(("_WIDTH", "_HEIGHT")):
            base, axis = ident[4:].rsplit("_", 1)
            dims.setdefault(base, {})[axis.lower()] = value
        elif in_index_section and ident != "TOTAL_MAPS" and not ident.endswith(("_SPRING", "_SUMMER", "_AUTUMN", "_WINTER")):
            names.setdefault(value, ident)
    return names, dims


def _segments(splat: str) -> dict[str, tuple[int, int]]:
    """Top-level splat segments: name -> (rom start, rom start of the next)."""
    starts: list[tuple[str, int]] = []
    current = None
    for line in splat.splitlines():
        if line.startswith("  - name: "):
            current = line[len("  - name: "):].strip()
        elif line.startswith("  - ["):
            current = None
            starts.append(("", int(line[len("  - ["):].split(",")[0].strip(" ]"), 0)))
        elif current and line.startswith("    start: "):
            starts.append((current, int(line.split(":", 1)[1].strip(), 0)))
            current = None
    segments = {}
    for (name, start), (_, end) in zip(starts, starts[1:]):
        if name:
            segments[name] = (start, end)	# bss segments have end == start
    return segments


def _animation_scripts(splat: str, rom: bytes) -> dict[str, list[int]]:
    """``data/animationScripts/*`` u16 tables; getAnimationOffsetFromScript
    masks each entry with 0x1FFF."""
    entries = [(int(m.group(1), 16), m.group(2)) for m in re.finditer(
        r"start: (0x[0-9A-Fa-f]+), type: animationScriptData, name: data/animationScripts/(\w+)", splat)]
    tables = {}
    for (start, name), (end, _) in zip(entries, entries[1:]):
        tables[name] = [v & 0x1FFF for v in struct.unpack_from(f">{(end - start) // 2}H", rom, start)]
    return tables


def _c_array(text: str, declaration: str) -> list[int]:
    begin = text.index(declaration)
    body = text[text.index("{", begin) + 1:text.index("};", begin)]
    return [int(value, 0) for value in re.findall(r"0x[0-9A-Fa-f]+|\b\d+\b", body)]


def _sprites(decomp: Path, rom: bytes) -> dict[str, object]:
    splat = (decomp / "config/us/splat.us.yaml").read_text()
    segments = _segments(splat)
    assets: dict[str, dict[str, object]] = {}
    for key, (texture, index, sheet, animations) in SPRITE_ASSETS.items():
        for name in filter(None, (texture, index, sheet)):
            if segments[name][1] <= segments[name][0]:
                raise SystemExit(f"splat segment {name} has no ROM data")
        entry: dict[str, object] = {"texture": ["0x%X" % v for v in segments[texture]],
                                    "index": ["0x%X" % v for v in segments[index]]}
        if animations is not None:
            entry["frames"] = [[a, f] for a, count in animations.items() for f in range(count)]
        if sheet is not None:
            entry["spritesheet"] = ["0x%X" % v for v in segments[sheet]]
        assets[key] = entry

    scripts = _animation_scripts(splat, rom)
    overlay = (decomp / "src/game/overlayScreens.c").read_text()
    items_c = (decomp / "src/game/items.c").read_text()
    tool_indices = _c_array(overlay, "toolAnimationIndices[]")
    key_indices = _c_array(overlay, "keyItemAnimationIndices[]")
    metadata = _c_array(items_c, "itemEntityMetadata[224][5]")
    if len(metadata) != 224 * 5:
        raise SystemExit(f"itemEntityMetadata has {len(metadata)} values, expected {224 * 5}")
    for tool in range(1, 6):
        if tool_indices[tool + 1] - tool_indices[tool] != TOOL_LEVEL_COUNTS[tool]:
            raise SystemExit(f"toolAnimationIndices no longer has {TOOL_LEVEL_COUNTS[tool]} levels for tool {tool}")
    # getItemAnimationIndex replaces the per-season index for these ranges.
    body = items_c[items_c.index("u16 getItemAnimationIndex(u16 index)"):]
    body = body[:body.index("return result;")]
    overrides = [(int(a), int(b), int(r)) for a, b, r in re.findall(
        r"if \((\d+) < index && index < (\d+)\) \{\s*result = (\d+);", body)]
    if len(overrides) != 3:
        raise SystemExit("getItemAnimationIndex changed; re-check the item icon mapping")

    def shop(index: int) -> int | None:
        return None if index == 0xFFFF else scripts["shopItems"][index]

    # Every slot of the decomp's tables, named or not: the runtime falls back
    # to "#id" names, an icon still helps.  Trailing 0x0000 key item entries
    # are padding.
    tool_icons = {}
    for tool in range(1, len(tool_indices)):
        count = TOOL_LEVEL_COUNTS.get(tool, 1)
        tool_icons[str(tool)] = [scripts["tools"][tool_indices[tool] + level] for level in range(count)]
    item_icons = {}
    for item in range(1, 224):
        row = metadata[5 * item:5 * item + 5]
        if all(v == 0 for v in row) or all(v == 0xFFFF for v in row[1:]):
            continue	# unused entry
        seasons = list(row[1:])
        for low, high, value in overrides:
            if low < item < high:
                seasons = [value] * 4
        item_icons[str(item)] = [shop(v) for v in seasons]
    key_icons = {str(k): shop(key_indices[k]) for k in range(1, len(key_indices)) if key_indices[k] != 0}
    return {
        "assets": assets,
        "palette_variants": PALETTE_VARIANTS,
        "icons": {
            # [level 0, level 1, ...]
            "tool": {"asset": "tools", "animations": tool_icons},
            # [spring, summer, autumn, winter] (gSeason 1..4)
            "item": {"asset": "items", "animations": item_icons},
            "key_item": {"asset": "items", "animations": key_icons},
        },
    }


def _define(text: str, name: str) -> int:
    match = re.search(r"^#define\s+%s\s+(0x[0-9A-Fa-f]+|\d+)\b" % re.escape(name), text, re.MULTILINE)
    if not match:
        raise SystemExit(f"#define {name} not found")
    return int(match.group(1), 0)


def _inventory_swap(decomp: Path) -> dict[str, int]:
    """When the pause screen could be opened, swapping two inventory bytes is
    what swapPauseScreenItems does.  Start is handled by handlePlayerInput:
    mainGameLoopCallback runs it only while the player entity is active,
    animated, map-space dependent and not paused (transition.c), and only in
    the CONTROLLER_INPUT action, not on the horse (player.c)."""
    items_h = (decomp / "src/game/items.h").read_text()
    if not re.search(r"s16 boulderHitPositionZ;\s*u8 toolUseState;", items_h):
        raise SystemExit("ToolUse layout changed; re-check TOOL_USE_STATE_OFFSET")
    player_h = (decomp / "src/game/player.h").read_text()
    entity_h = (decomp / "src/system/entity.h").read_text()
    if not re.search(r"u16 flags; // 0x6A\s*\} Entity;", entity_h):
        raise SystemExit("Entity layout changed; re-check the entity flags offset")
    if _define((decomp / "src/assetIndices/entities.h").read_text(), "ENTITY_PLAYER") != 0:
        raise SystemExit("ENTITY_PLAYER is no longer entity 0")
    return {
        "player_entity_flags_offset": 0x6A,
        "entity_active": _define(entity_h, "ENTITY_ACTIVE"),
        "entity_animated": _define(entity_h, "ENTITY_IS_CURRENTLY_ANIMATED"),
        "entity_paused": _define(entity_h, "ENTITY_PAUSED"),
        "entity_map_space_independent": _define(entity_h, "ENTITY_MAP_SPACE_INDEPENDENT"),
        "main_loop_main_game": _define((decomp / "src/mainLoop.h").read_text(), "MAIN_GAME"),
        "action_controller_input": _define(player_h, "CONTROLLER_INPUT"),
        "flag_riding_horse": _define(player_h, "PLAYER_RIDING_HORSE"),
        "tool_use_state_offset": TOOL_USE_STATE_OFFSET,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--decomp", type=Path, required=True)
    parser.add_argument("--syms", type=Path, required=True)
    parser.add_argument("--rom", type=Path, required=True)
    args = parser.parse_args()

    rom = args.rom.read_bytes()
    rom_sha1 = hashlib.sha1(rom).hexdigest()
    decomp_sha1 = re.search(r"sha1:\s*([0-9a-f]{40})", (args.decomp / "decomp.yaml").read_text()).group(1)
    if rom_sha1 != decomp_sha1:
        raise SystemExit(f"ROM SHA-1 {rom_sha1} does not match the decomp baserom {decomp_sha1}")

    syms_text = args.syms.read_text()
    symbols: dict[str, str] = {}
    for name in SYMBOLS:
        match = re.search(r'name = "%s", vram = (0x[0-9A-Fa-f]+)' % re.escape(name), syms_text)
        if not match:
            raise SystemExit(f"symbol {name} not found in {args.syms}")
        symbols[name] = "0x%08X" % int(match.group(1), 16)

    items_h = (args.decomp / "src/game/items.h").read_text()
    map_names, map_dims = _maps((args.decomp / "src/assetIndices/maps.h").read_text())
    maps = {}
    for index, ident in sorted(map_names.items()):
        entry: dict[str, object] = {"name": _pretty(ident)}
        if ident in map_dims and {"width", "height"} <= map_dims[ident].keys():
            entry["width_tiles"] = map_dims[ident]["width"]
            entry["height_tiles"] = map_dims[ident]["height"]
        maps[str(index)] = entry

    try:
        commit = subprocess.run(["git", "-C", str(args.decomp), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"

    profile = {
        "id": "harvest_moon_64.us",
        "extension": "harvest_moon_64",
        "game": "Harvest Moon 64 (USA)",
        "rom": {
            "sha1": rom_sha1,
            "sha256": hashlib.sha256(rom).hexdigest(),
            "crc1": "0x%08X" % int.from_bytes(rom[0x10:0x14], "big"),
            "crc2": "0x%08X" % int.from_bytes(rom[0x14:0x18], "big"),
        },
        "source": f"hm64-decomp {commit}: src/game/player.h, items.h, items.c, overlayScreens.c, "
                  "time.h, game.h, level.h, src/assetIndices/maps.h, config/us/splat.us.yaml; "
                  "symbol addresses from hm64.us.datasyms.toml; animation scripts read from the ROM",
        "symbols": symbols,
        "player_layout": _player_layout(),
        "names": {
            "tool": {str(k): v for k, v in sorted(_section_defines(items_h, "/* tools */", "/* key/special items */").items())},
            "key_item": {str(k): v for k, v in sorted(_section_defines(items_h, "/* key/special items */", "/* item indices */").items())},
            "item": {str(k): v for k, v in sorted(_section_defines(items_h, "/* item indices */", "/* bottle contents */").items())},
            "season": {"1": "Spring", "2": "Summer", "3": "Autumn", "4": "Winter"},
            "weekday": {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat"},
        },
        "maps": maps,
        # Pause-screen graphics for the bottom screen.  Only ROM ranges and
        # animation numbers: the builder decodes the images from the user's
        # ROM (tools/n64to3ds/hm64_sprites.py).
        "sprites": None,
        # world = map_tile_size * (tile - dimension / 2), centre tile = 0
        # (TILE_TO_WORLD_X/Z in src/assetIndices/maps.h); X and Z are the
        # ground plane.
        "map_tile_size": 32,
    }
    profile["sprites"] = _sprites(args.decomp, rom)
    profile["inventory_swap"] = _inventory_swap(args.decomp)
    PROFILE_PATH.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {PROFILE_PATH}")


if __name__ == "__main__":
    main()

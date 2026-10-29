"""Harvest Moon 64 sprite assets -> texture atlas for the bottom screen.

The Harvest Moon 64 extension draws its bottom screen with the game's own
pause-screen graphics.  They are decoded here, at build time, from the ROM the
user supplied, and only ever written into the CIA's RomFS; nothing decoded is
committed.  Which ROM segments hold which sprites, and which animation shows
which item, are verified facts in the profile (``sprites`` section, generated
from hm64-decomp by ``profiles/generate_hm64_profile.py``).

Sprite format (hm64-decomp src/system/sprite.c, globalSprites.c, graphic.c):

* An *assets index* segment holds big-endian u32 offsets into the texture
  segment: ``[spritesheet, palettes, animations, sprite-to-palette, end]``.
* Textures are found through a u32 offset table: inside the texture segment
  at the spritesheet offset (relative to it), or, for "type 1" assets, in a
  separate *spritesheet index* segment (relative to the texture segment).
  Each texture is ``u16 header, u16 flags (BE), u16 width (LE), u16 height
  (LE)`` followed by CI4 (``(flags >> 4) & 0xF == 1``) or CI8 pixels.
* Palettes: u32 offset table; each palette is a 4-byte header (bits 4..7 of
  the second half-word: 1 = 16 colours, otherwise 256) and RGBA5551 colours.
* Sprite-to-palette: 4-byte header, then one palette index per texture.
* Animations: u32 offset table; an animation is an 8-byte header (half-word 2
  is the little-endian frame count), then per frame ``objectCount (LE),
  duration`` and ``objectCount`` objects of ``texture (LE), unused,
  anchorX (LE), anchorY (LE)``.  Object 0 is drawn on top.

Placement: a texture of size w x h with anchor (ax, ay) covers
``x = ax - w // 2 .. + w`` and ``y = ay - h // 2 .. + h`` relative to the
sprite's position, with y pointing down (the game uses y up and the negated
anchor, setupBitmapVertices with centred alignment).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BuildError

ATLAS_FILE = Path("dualscreen") / "atlas.rgba"
# Transparent border around each packed image, so bilinear sampling of a
# rotated or scaled sprite never picks up its neighbour.
PADDING = 1
# picaGL needs power-of-two textures; 1024 is the PICA200 limit.
ATLAS_SIZES = ((256, 256), (512, 256), (512, 512), (1024, 512), (1024, 1024))


@dataclass(frozen=True)
class Image:
    width: int
    height: int
    rgba: bytes
    # Position of the sprite's origin relative to the image's top-left pixel.
    origin_x: int
    origin_y: int


def _parse_range(value: Any, what: str) -> tuple[int, int]:
    try:
        start, end = (int(str(v), 0) for v in value)
    except (TypeError, ValueError) as exc:
        raise BuildError(f"Invalid ROM range for {what}: {value!r}") from exc
    if not 0 <= start < end:
        raise BuildError(f"Invalid ROM range for {what}: {value!r}")
    return start, end


class SpriteAsset:
    """One sprite asset (texture + assets index [+ spritesheet index])."""

    def __init__(self, name: str, rom: bytes, texture: tuple[int, int], index: tuple[int, int],
                 spritesheet: tuple[int, int] | None = None):
        self.name = name
        for what, (start, end) in (("texture", texture), ("index", index), ("spritesheet", spritesheet or (0, 1))):
            if end > len(rom):
                raise BuildError(f"Sprite asset {name}: {what} range 0x{start:X}-0x{end:X} is outside the ROM.")
        self.segment = rom[texture[0]:texture[1]]
        index_data = rom[index[0]:index[1]]
        if len(index_data) < 20:
            raise BuildError(f"Sprite asset {name}: assets index is too short.")
        offsets = struct.unpack_from(">5I", index_data)
        if list(offsets) != sorted(offsets) or offsets[4] > len(self.segment):
            raise BuildError(f"Sprite asset {name}: inconsistent assets index {offsets}.")
        self.palettes = self.segment[offsets[1]:offsets[2]]
        self.animations = self.segment[offsets[2]:offsets[3]]
        self.sprite_to_palette = self.segment[offsets[3]:offsets[4]]
        if spritesheet is None:
            sheet = self.segment[offsets[0]:offsets[1]]
            self.texture_offsets = [offsets[0] + o for o in self._table(sheet, "spritesheet")]
        else:
            table = rom[spritesheet[0]:spritesheet[1]]
            self.texture_offsets = list(struct.unpack_from(f">{len(table) // 4}I", table))
        self.palette_offsets = self._table(self.palettes, "palette")
        self.animation_offsets = self._table(self.animations, "animation")

    def _table(self, data: bytes, what: str) -> list[int]:
        if len(data) < 4:
            raise BuildError(f"Sprite asset {self.name}: empty {what} table.")
        count = struct.unpack_from(">I", data)[0] // 4
        if count == 0 or count * 4 > len(data):
            raise BuildError(f"Sprite asset {self.name}: invalid {what} table.")
        return list(struct.unpack_from(f">{count}I", data))

    def _check(self, data: bytes, offset: int, size: int, what: str) -> None:
        if offset < 0 or offset + size > len(data):
            raise BuildError(f"Sprite asset {self.name}: {what} is outside its segment.")

    def palette(self, index: int) -> list[tuple[int, int, int, int]]:
        if index >= len(self.palette_offsets):
            raise BuildError(f"Sprite asset {self.name}: palette {index} does not exist.")
        offset = self.palette_offsets[index]
        self._check(self.palettes, offset, 4, f"palette {index}")
        count = 16 if (struct.unpack_from(">H", self.palettes, offset + 2)[0] >> 4) & 0xF == 1 else 256
        # The last palette of a segment may be shorter than its nominal size;
        # missing entries are never referenced by valid pixels.
        available = min(count, (len(self.palettes) - offset - 4) // 2)
        colours = []
        for value in struct.unpack_from(f">{available}H", self.palettes, offset + 4):
            colours.append((((value >> 11) & 31) * 255 // 31, ((value >> 6) & 31) * 255 // 31,
                            ((value >> 1) & 31) * 255 // 31, 255 if value & 1 else 0))
        return colours + [(0, 0, 0, 0)] * (count - available)

    def texture_palette(self, texture: int) -> int:
        self._check(self.sprite_to_palette, 4 + texture, 1, f"palette of texture {texture}")
        return self.sprite_to_palette[4 + texture]

    def texture(self, index: int, palette: int | None = None) -> tuple[int, int, bytes]:
        if index >= len(self.texture_offsets):
            raise BuildError(f"Sprite asset {self.name}: texture {index} does not exist.")
        offset = self.texture_offsets[index]
        self._check(self.segment, offset, 8, f"texture {index}")
        flags = struct.unpack_from(">H", self.segment, offset + 2)[0]
        width, height = struct.unpack_from("<HH", self.segment, offset + 4)
        ci4 = (flags >> 4) & 0xF == 1
        size = (width * height + 1) // 2 if ci4 else width * height
        self._check(self.segment, offset + 8, size, f"texture {index} pixels")
        pixels = self.segment[offset + 8:offset + 8 + size]
        colours = self.palette(self.texture_palette(index) if palette is None else palette)
        out = bytearray(width * height * 4)
        for i in range(width * height):
            if ci4:
                byte = pixels[i >> 1]
                colour = byte >> 4 if i % 2 == 0 else byte & 15
            else:
                colour = pixels[i]
            out[4 * i:4 * i + 4] = bytes(colours[colour])
        return width, height, bytes(out)

    def frames(self, animation: int) -> list[list[tuple[int, int, int]]]:
        """Objects ``(texture, anchor_x, anchor_y)`` of every frame."""
        if animation >= len(self.animation_offsets):
            raise BuildError(f"Sprite asset {self.name}: animation {animation} does not exist.")
        base = self.animation_offsets[animation]
        if base + 8 > len(self.animations):
            return []	# trailing entries of the table point at its end
        data = self.animations
        frame_count = struct.unpack_from("<H", data, base + 4)[0]
        position = base + 8
        frames = []
        for _ in range(frame_count):
            self._check(data, position, 4, f"animation {animation}")
            objects = struct.unpack_from("<H", data, position)[0]
            position += 4
            self._check(data, position, objects * 8, f"animation {animation}")
            frame = []
            for _ in range(objects):
                texture, _unused, anchor_x, anchor_y = struct.unpack_from("<HHhh", data, position)
                frame.append((texture, anchor_x, anchor_y))
                position += 8
            frames.append(frame)
        return frames

    def compose(self, animation: int, frame: int = 0, palette: int | None = None) -> Image | None:
        """One animation frame as a single image, trimmed to its opaque pixels."""
        frames = self.frames(animation)
        if frame >= len(frames) or not frames[frame]:
            return None
        layers = []
        for texture, anchor_x, anchor_y in frames[frame]:
            width, height, rgba = self.texture(texture, palette)
            layers.append((anchor_x - width // 2, anchor_y - height // 2, width, height, rgba))
        left = min(x for x, _, _, _, _ in layers)
        top = min(y for _, y, _, _, _ in layers)
        width = max(x + w for x, _, w, _, _ in layers) - left
        height = max(y + h for _, y, _, h, _ in layers) - top
        canvas = bytearray(width * height * 4)
        for x0, y0, w, h, rgba in reversed(layers):	# object 0 ends up on top
            for y in range(h):
                for x in range(w):
                    pixel = rgba[4 * (y * w + x):4 * (y * w + x) + 4]
                    if pixel[3]:
                        target = 4 * ((y0 - top + y) * width + x0 - left + x)
                        canvas[target:target + 4] = pixel
        return _trim(Image(width, height, bytes(canvas), -left, -top))


def _trim(image: Image) -> Image | None:
    rows = [y for y in range(image.height)
            if any(image.rgba[4 * (y * image.width + x) + 3] for x in range(image.width))]
    if not rows:
        return None
    cols = [x for x in range(image.width)
            if any(image.rgba[4 * (y * image.width + x) + 3] for y in rows)]
    x0, x1, y0, y1 = cols[0], cols[-1] + 1, rows[0], rows[-1] + 1
    width = x1 - x0
    rgba = b"".join(image.rgba[4 * (y * image.width + x0):4 * (y * image.width + x1)] for y in range(y0, y1))
    return Image(width, y1 - y0, rgba, image.origin_x - x0, image.origin_y - y0)


@dataclass(frozen=True)
class Atlas:
    width: int
    height: int
    rgba: bytes
    # name -> (x, y, width, height, origin_x, origin_y)
    entries: dict[str, tuple[int, int, int, int, int, int]]


def pack(images: dict[str, Image]) -> Atlas:
    """Shelf-pack images into the smallest power-of-two atlas that fits."""
    order = sorted(images, key=lambda name: (-images[name].height, -images[name].width, name))
    for atlas_width, atlas_height in ATLAS_SIZES:
        placements: dict[str, tuple[int, int]] = {}
        x = y = shelf = 0
        for name in order:
            w = images[name].width + 2 * PADDING
            h = images[name].height + 2 * PADDING
            if w > atlas_width:
                break
            if x + w > atlas_width:
                x, y, shelf = 0, y + shelf, 0
            if y + h > atlas_height:
                break
            placements[name] = (x + PADDING, y + PADDING)
            x += w
            shelf = max(shelf, h)
        if len(placements) == len(images):
            break
    else:
        raise BuildError("Harvest Moon 64 sprites do not fit into a 1024x1024 atlas.")

    pixels = bytearray(atlas_width * atlas_height * 4)
    entries = {}
    for name, (x, y) in sorted(placements.items()):
        image = images[name]
        for row in range(image.height):
            target = 4 * ((y + row) * atlas_width + x)
            pixels[target:target + 4 * image.width] = image.rgba[4 * row * image.width:4 * (row + 1) * image.width]
        entries[name] = (x, y, image.width, image.height, image.origin_x, image.origin_y)
    return Atlas(atlas_width, atlas_height, bytes(pixels), entries)


def _icon_animations(sprites: dict[str, Any]) -> dict[str, set[int]]:
    wanted: dict[str, set[int]] = {}
    for category, table in sprites.get("icons", {}).items():
        asset = table.get("asset")
        if asset not in sprites.get("assets", {}):
            raise BuildError(f"Icon table {category!r} refers to unknown sprite asset {asset!r}.")
        for values in table.get("animations", {}).values():
            for value in values if isinstance(values, list) else [values]:
                if value is not None:
                    wanted.setdefault(asset, set()).add(int(value))
    return wanted


def entry_name(asset: str, animation: int, frame: int, palette: int | None = None) -> str:
    name = f"{asset}.{animation}.{frame}"
    return name if palette is None else f"{name}@{palette}"


def build_atlas(rom: bytes, sprites: dict[str, Any]) -> Atlas:
    """Decode the sprites a profile lists and pack them into one atlas.

    An asset's ``frames`` list (``[animation, frame]`` pairs: pause-screen
    clock, digits, grids) must all decode; icon tables add frame 0 of the
    animations they reference.
    """
    assets: dict[str, SpriteAsset] = {}
    for name, spec in sprites.get("assets", {}).items():
        spritesheet = spec.get("spritesheet")
        assets[name] = SpriteAsset(
            name, rom, _parse_range(spec.get("texture"), f"{name}.texture"),
            _parse_range(spec.get("index"), f"{name}.index"),
            _parse_range(spritesheet, f"{name}.spritesheet") if spritesheet is not None else None)

    images: dict[str, Image] = {}

    def add(asset: str, animation: int, frame: int, palette: int | None = None) -> None:
        image = assets[asset].compose(animation, frame, palette)
        if image is not None:
            images[entry_name(asset, animation, frame, palette)] = image

    icons = _icon_animations(sprites)
    for name, spec in sprites.get("assets", {}).items():
        for animation, frame in spec.get("frames", []):
            add(name, int(animation), int(frame))
            if entry_name(name, int(animation), int(frame)) not in images:
                raise BuildError(f"Sprite asset {name}: frame {frame} of animation {animation} is empty.")
        for animation in sorted(icons.get(name, ())):
            add(name, animation, 0)
    for asset, animation, frame, palette in sprites.get("palette_variants", []):
        if asset not in assets:
            raise BuildError(f"Palette variant refers to unknown sprite asset {asset!r}.")
        add(asset, int(animation), int(frame), int(palette))
    if not images:
        raise BuildError("The profile's sprite section produced no images.")
    return pack(images)


def profile_lines(atlas: Atlas, sprites: dict[str, Any]) -> list[str]:
    """Runtime ``key=value`` lines describing the atlas and the icon tables."""
    lines = [f"atlas.file=romfs:/{ATLAS_FILE.as_posix()}", f"atlas.width={atlas.width}",
             f"atlas.height={atlas.height}"]
    for name, rect in sorted(atlas.entries.items()):
        lines.append(f"sprite.{name}=" + ",".join(str(v) for v in rect))
    for category, table in sorted(sprites.get("icons", {}).items()):
        lines.append(f"icon.{category}.asset={table['asset']}")
        for key, values in sorted(table.get("animations", {}).items(), key=lambda item: int(item[0])):
            values = values if isinstance(values, list) else [values]
            lines.append(f"icon.{category}.{int(key)}=" + ",".join("-1" if v is None else str(int(v)) for v in values))
    return lines


def write_atlas(rom: bytes, sprites: dict[str, Any], romfs_dir: Path) -> list[str]:
    atlas = build_atlas(rom, sprites)
    destination = romfs_dir / ATLAS_FILE
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(atlas.rgba)
    return profile_lines(atlas, sprites)

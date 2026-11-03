"""HOME Menu artwork for generated CIAs: the SMDH icon and the banner.

The HOME Menu shows a title's SMDH name and 48x48 icon in the grid and its
banner (a small 3D scene) on the top screen.  n64to3ds used to ship
DaedalusX64's own icon and banner for every game.  This module draws a
game-specific icon and banner texture with the game's name instead, or uses
PNG images the user supplies.

Everything is pure Python: the release bundle ships a bare CPython without an
imaging library.  Text is rasterised from the Roboto font that Daedalus
already embeds in RomFS.

The banner keeps DaedalusX64's banner model and jingle and only replaces its
256x128 texture: the CGFX scene is LZ11-compressed inside a CBMD container,
and its single image texture is RGBA4 in the PICA200 8x8 Morton-tiled layout.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from .errors import BuildError

ICON_SIZE = 48
BANNER_WIDTH = 256
BANNER_HEIGHT = 128

# Nintendo 64 logo colours; one is picked per game from its ROM hash.
_PALETTE = (
    (214, 40, 40),
    (0, 146, 70),
    (32, 80, 190),
    (222, 150, 16),
)

# --------------------------------------------------------------------------
# TrueType outlines


class TrueTypeFont:
    """The subset of a TrueType font needed to draw ASCII-ish titles."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        try:
            num_tables = struct.unpack_from(">H", data, 4)[0]
            self.tables: dict[str, tuple[int, int]] = {}
            for i in range(num_tables):
                tag, _checksum, offset, length = struct.unpack_from(">4sIII", data, 12 + 16 * i)
                self.tables[tag.decode("latin-1")] = (offset, length)
            for required in ("head", "hhea", "hmtx", "loca", "glyf", "cmap", "maxp"):
                if required not in self.tables:
                    raise ValueError(f"the font has no {required!r} table")
            head = self.tables["head"][0]
            self.units_per_em = struct.unpack_from(">H", data, head + 18)[0]
            self.long_loca = struct.unpack_from(">h", data, head + 50)[0] == 1
            hhea = self.tables["hhea"][0]
            self.ascender, self.descender = struct.unpack_from(">hh", data, hhea + 4)
            self.num_hmetrics = struct.unpack_from(">H", data, hhea + 34)[0]
            self.num_glyphs = struct.unpack_from(">H", data, self.tables["maxp"][0] + 4)[0]
            self._cmap = self._parse_cmap()
        except (struct.error, ValueError) as exc:
            raise BuildError(f"Cannot read the HOME Menu artwork font: {exc}") from exc
        self._contours: dict[int, list[list[tuple[float, float, bool]]]] = {}

    @classmethod
    def load(cls, path: Path) -> "TrueTypeFont":
        return cls(Path(path).read_bytes())

    def _parse_cmap(self):
        data = self.data
        base = self.tables["cmap"][0]
        count = struct.unpack_from(">H", data, base + 2)[0]
        best = None
        for i in range(count):
            platform, encoding, offset = struct.unpack_from(">HHI", data, base + 4 + 8 * i)
            if struct.unpack_from(">H", data, base + offset)[0] != 4:
                continue
            if (platform, encoding) == (3, 1):
                best = base + offset
                break
            if platform == 0 and best is None:
                best = base + offset
        if best is None:
            raise ValueError("the font has no Unicode format 4 cmap")
        seg_count = struct.unpack_from(">H", data, best + 6)[0] // 2
        ends = struct.unpack_from(f">{seg_count}H", data, best + 14)
        starts = struct.unpack_from(f">{seg_count}H", data, best + 16 + 2 * seg_count)
        deltas = struct.unpack_from(f">{seg_count}h", data, best + 16 + 4 * seg_count)
        range_base = best + 16 + 6 * seg_count
        range_offsets = struct.unpack_from(f">{seg_count}H", data, range_base)
        return starts, ends, deltas, range_offsets, range_base

    def glyph_index(self, code: int) -> int:
        starts, ends, deltas, range_offsets, range_base = self._cmap
        for i, end in enumerate(ends):
            if code > end:
                continue
            if code < starts[i]:
                return 0
            if range_offsets[i] == 0:
                return (code + deltas[i]) & 0xFFFF
            address = range_base + 2 * i + range_offsets[i] + 2 * (code - starts[i])
            glyph = struct.unpack_from(">H", self.data, address)[0]
            return (glyph + deltas[i]) & 0xFFFF if glyph else 0
        return 0

    def advance(self, glyph: int) -> int:
        hmtx = self.tables["hmtx"][0]
        return struct.unpack_from(">H", self.data, hmtx + 4 * min(glyph, self.num_hmetrics - 1))[0]

    def _glyph_range(self, glyph: int) -> tuple[int, int]:
        loca = self.tables["loca"][0]
        if self.long_loca:
            start, end = struct.unpack_from(">II", self.data, loca + 4 * glyph)
        else:
            start, end = struct.unpack_from(">HH", self.data, loca + 2 * glyph)
            start, end = start * 2, end * 2
        return self.tables["glyf"][0] + start, end - start

    def contours(self, glyph: int, depth: int = 0) -> list[list[tuple[float, float, bool]]]:
        """Outline contours in font units, y up: lists of (x, y, on_curve)."""

        cached = self._contours.get(glyph)
        if cached is not None:
            return cached
        result: list[list[tuple[float, float, bool]]] = []
        if 0 <= glyph < self.num_glyphs:
            start, length = self._glyph_range(glyph)
            if length > 0:
                count = struct.unpack_from(">h", self.data, start)[0]
                if count >= 0:
                    result = self._simple_glyph(start + 10, count)
                elif depth < 8:
                    result = self._composite_glyph(start + 10, depth)
        self._contours[glyph] = result
        return result

    def _simple_glyph(self, pos: int, num_contours: int):
        data = self.data
        if num_contours == 0:
            return []
        ends = struct.unpack_from(f">{num_contours}H", data, pos)
        pos += 2 * num_contours
        pos += 2 + struct.unpack_from(">H", data, pos)[0]  # skip instructions
        num_points = ends[-1] + 1
        flags: list[int] = []
        while len(flags) < num_points:
            flag = data[pos]
            pos += 1
            flags.append(flag)
            if flag & 0x08:
                flags.extend([flag] * data[pos])
                pos += 1
        flags = flags[:num_points]
        xs: list[int] = []
        value = 0
        for flag in flags:
            if flag & 0x02:
                value += data[pos] if flag & 0x10 else -data[pos]
                pos += 1
            elif not flag & 0x10:
                value += struct.unpack_from(">h", data, pos)[0]
                pos += 2
            xs.append(value)
        ys: list[int] = []
        value = 0
        for flag in flags:
            if flag & 0x04:
                value += data[pos] if flag & 0x20 else -data[pos]
                pos += 1
            elif not flag & 0x20:
                value += struct.unpack_from(">h", data, pos)[0]
                pos += 2
            ys.append(value)
        contours = []
        first = 0
        for end in ends:
            contours.append([(float(xs[i]), float(ys[i]), bool(flags[i] & 1)) for i in range(first, end + 1)])
            first = end + 1
        return contours

    def _composite_glyph(self, pos: int, depth: int):
        data = self.data
        contours = []
        while True:
            flags, component = struct.unpack_from(">HH", data, pos)
            pos += 4
            if flags & 0x0001:
                arg1, arg2 = struct.unpack_from(">hh", data, pos)
                pos += 4
            else:
                arg1, arg2 = struct.unpack_from(">bb", data, pos)
                pos += 2
            xx, xy, yx, yy = 1.0, 0.0, 0.0, 1.0
            if flags & 0x0008:
                xx = yy = struct.unpack_from(">h", data, pos)[0] / 16384.0
                pos += 2
            elif flags & 0x0040:
                xx, yy = (v / 16384.0 for v in struct.unpack_from(">hh", data, pos))
                pos += 4
            elif flags & 0x0080:
                xx, xy, yx, yy = (v / 16384.0 for v in struct.unpack_from(">hhhh", data, pos))
                pos += 8
            # Point-matched placement (ARGS_ARE_XY_VALUES clear) is rare in
            # Latin glyphs; such components are placed at the origin.
            dx, dy = (arg1, arg2) if flags & 0x0002 else (0, 0)
            for contour in self.contours(component, depth + 1):
                contours.append([(x * xx + y * yx + dx, x * xy + y * yy + dy, on) for x, y, on in contour])
            if not flags & 0x0020:
                break
        return contours

    def text_width(self, text: str, size: float) -> float:
        scale = size / self.units_per_em
        return sum(self.advance(self.glyph_index(ord(ch))) for ch in text) * scale


def _flatten(contour: list[tuple[float, float, bool]], steps: int) -> list[tuple[float, float]]:
    """Quadratic TrueType contour to a closed polygon."""

    count = len(contour)
    if count < 2:
        return []
    start = next((i for i, point in enumerate(contour) if point[2]), None)
    if start is None:
        first, second = contour[0], contour[1]
        midpoint = ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2, True)
        ordered = [midpoint] + contour[1:] + [contour[0]]
    else:
        ordered = contour[start:] + contour[:start]

    out = [(ordered[0][0], ordered[0][1])]
    current = ordered[0]
    control = None

    def quad(p0, p1, p2):
        for step in range(1, steps + 1):
            t = step / steps
            u = 1 - t
            out.append(
                (
                    u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1],
                )
            )

    for point in ordered[1:] + [ordered[0]]:
        if point[2]:
            if control is None:
                out.append((point[0], point[1]))
            else:
                quad(current, control, point)
                control = None
            current = point
        elif control is None:
            control = point
        else:
            midpoint = ((control[0] + point[0]) / 2, (control[1] + point[1]) / 2, True)
            quad(current, control, midpoint)
            current = midpoint
            control = point
    return out


def _fill_polygons(polygons: list[list[tuple[float, float]]], width: int, height: int, supersample: int) -> list[float]:
    """Non-zero winding coverage (0..1) per pixel; polygon coordinates are
    pixels, y down."""

    ss = supersample
    edges = []
    for polygon in polygons:
        count = len(polygon)
        for i in range(count):
            x0, y0 = polygon[i]
            x1, y1 = polygon[(i + 1) % count]
            x0, y0, x1, y1 = x0 * ss, y0 * ss, x1 * ss, y1 * ss
            if y0 == y1:
                continue
            direction = 1
            if y0 > y1:
                x0, y0, x1, y1 = x1, y1, x0, y0
                direction = -1
            edges.append((y0, y1, x0, (x1 - x0) / (y1 - y0), direction))
    edges.sort()
    counts = [0] * (width * height)
    sample_width = width * ss
    next_edge = 0
    active: list[tuple[float, float, float, float, int]] = []
    for row in range(height * ss):
        center = row + 0.5
        while next_edge < len(edges) and edges[next_edge][0] <= center:
            active.append(edges[next_edge])
            next_edge += 1
        active = [edge for edge in active if edge[1] > center]
        if not active:
            continue
        crossings = sorted(
            (edge[2] + (center - edge[0]) * edge[3], edge[4]) for edge in active if edge[0] <= center
        )
        winding = 0
        out_row = (row // ss) * width
        for i in range(len(crossings) - 1):
            winding += crossings[i][1]
            if winding == 0:
                continue
            # Sample columns whose centres lie in [left, right).
            left = max(0, int(crossings[i][0] - 0.5 + 0.999999))
            right = min(sample_width, int(crossings[i + 1][0] - 0.5 + 0.999999))
            column = left
            while column < right:
                pixel = column // ss
                stop = min(right, (pixel + 1) * ss)
                counts[out_row + pixel] += stop - column
                column = stop
    area = float(ss * ss)
    return [c / area for c in counts]


# --------------------------------------------------------------------------
# A small RGBA canvas (straight alpha, floats 0..1)


class Canvas:
    def __init__(self, width: int, height: int, fill=(0.0, 0.0, 0.0, 0.0)) -> None:
        self.width = width
        self.height = height
        self.pixels = [list(fill) for _ in range(width * height)]

    def blend(self, index: int, red: float, green: float, blue: float, alpha: float) -> None:
        if alpha <= 0.0:
            return
        if alpha > 1.0:
            alpha = 1.0
        pixel = self.pixels[index]
        keep = pixel[3] * (1.0 - alpha)
        out = alpha + keep
        pixel[0] = (red * alpha + pixel[0] * keep) / out
        pixel[1] = (green * alpha + pixel[1] * keep) / out
        pixel[2] = (blue * alpha + pixel[2] * keep) / out
        pixel[3] = out

    def fill(self, mask: list[float], color, alpha: float = 1.0) -> None:
        red, green, blue = color
        for index, coverage in enumerate(mask):
            if coverage > 0.0:
                self.blend(index, red, green, blue, coverage * alpha)

    def fill_vertical_gradient(self, mask: list[float], top, bottom, y0: float, y1: float) -> None:
        span = max(1e-6, y1 - y0)
        for y in range(self.height):
            t = min(1.0, max(0.0, (y + 0.5 - y0) / span))
            color = [top[c] + (bottom[c] - top[c]) * t for c in range(3)]
            base = y * self.width
            for x in range(self.width):
                coverage = mask[base + x]
                if coverage > 0.0:
                    self.blend(base + x, color[0], color[1], color[2], coverage)

    def rgba_bytes(self, opaque_background=None) -> bytes:
        out = bytearray(self.width * self.height * 4)
        for index, (red, green, blue, alpha) in enumerate(self.pixels):
            if opaque_background is not None:
                keep = 1.0 - alpha
                red = red * alpha + opaque_background[0] * keep
                green = green * alpha + opaque_background[1] * keep
                blue = blue * alpha + opaque_background[2] * keep
                alpha = 1.0
            out[index * 4 : index * 4 + 4] = bytes(
                min(255, max(0, int(value * 255.0 + 0.5))) for value in (red, green, blue, alpha)
            )
        return bytes(out)


def _rounded_rect_mask(width: int, height: int, x0: float, y0: float, x1: float, y1: float, radius: float) -> list[float]:
    def inside(px: float, py: float) -> bool:
        if px < x0 or px > x1 or py < y0 or py > y1:
            return False
        cx = min(max(px, x0 + radius), x1 - radius)
        cy = min(max(py, y0 + radius), y1 - radius)
        return (px - cx) ** 2 + (py - cy) ** 2 <= radius * radius

    mask = [0.0] * (width * height)
    ss = 4
    for y in range(height):
        for x in range(width):
            corners = inside(x, y) + inside(x + 1, y) + inside(x, y + 1) + inside(x + 1, y + 1)
            if corners == 4:
                mask[y * width + x] = 1.0  # the shape is convex
            elif corners or (x + 1 > x0 and x < x1 and y + 1 > y0 and y < y1):
                hits = sum(
                    inside(x + (i + 0.5) / ss, y + (j + 0.5) / ss) for i in range(ss) for j in range(ss)
                )
                mask[y * width + x] = hits / (ss * ss)
    return mask


def _dilate(mask: list[float], width: int, height: int, radius: int) -> list[float]:
    offsets = [
        (dx, dy)
        for dx in range(-radius, radius + 1)
        for dy in range(-radius, radius + 1)
        if dx * dx + dy * dy <= radius * radius + radius
    ]
    out = list(mask)
    for dx, dy in offsets:
        for y in range(max(0, -dy), min(height, height - dy)):
            source = (y + dy) * width
            target = y * width
            for x in range(max(0, -dx), min(width, width - dx)):
                value = mask[source + x + dx]
                if value > out[target + x]:
                    out[target + x] = value
    return out


# --------------------------------------------------------------------------
# Text layout


def _split_camel(word: str) -> list[str]:
    parts = []
    current = ""
    for i, ch in enumerate(word):
        if current and ch.isupper() and (not word[i - 1].isupper()):
            parts.append(current)
            current = ch
        else:
            current += ch
    if current:
        parts.append(current)
    return parts


def _wrap(font: TrueTypeFont, words: list[str], size: float, max_width: float) -> list[str] | None:
    lines: list[str] = []
    current = ""
    queue = list(words)
    while queue:
        word = queue.pop(0)
        candidate = f"{current} {word}" if current else word
        if font.text_width(candidate, size) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
            queue.insert(0, word)
            continue
        parts = _split_camel(word)
        if len(parts) > 1:
            queue[0:0] = parts
            continue
        return None
    if current:
        lines.append(current)
    return lines


def _abbreviation(title: str) -> str:
    letters = ""
    for word in title.split():
        if word.isdigit():
            letters += word
        else:
            letters += "".join(part[0] for part in _split_camel(word) if part[:1].isalnum())
    return letters.upper()[:6] or "N64"


@dataclass
class _Layout:
    size: float
    lines: list[str]


def _short_titles(title: str) -> list[str]:
    """The title, then shorter forms of it for a small icon."""

    candidates = [title]
    stripped = title[4:] if title.lower().startswith("the ") else title
    candidates.append(stripped)
    for separator in (": ", " - "):
        if separator in stripped:
            head, tail = stripped.split(separator, 1)
            candidates.extend((tail, head))
    unique = []
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


def _fit_text(
    font: TrueTypeFont,
    title: str,
    max_width: float,
    max_height: float,
    max_lines: int,
    largest: int,
    smallest: int,
) -> _Layout:
    for candidate in _short_titles(title):
        words = candidate.split() or [candidate]
        for size in range(largest, smallest - 1, -1):
            lines = _wrap(font, words, size, max_width)
            if lines is None or len(lines) > max_lines:
                continue
            if _block_height(size, len(lines)) <= max_height:
                return _Layout(size, lines)
    short = _abbreviation(title)
    for size in range(largest, 5, -1):
        if font.text_width(short, size) <= max_width and _block_height(size, 1) <= max_height:
            return _Layout(size, [short])
    return _Layout(6, [short])


def _block_height(size: float, lines: int) -> float:
    # Cap height plus line pitch; Roboto's cap height is about 0.71 em.
    return 0.72 * size + (lines - 1) * 1.08 * size


def _text_mask(
    font: TrueTypeFont,
    layout: _Layout,
    width: int,
    height: int,
    box: tuple[float, float, float, float],
    *,
    tracking: float = 0.0,
) -> list[float]:
    x0, y0, x1, y1 = box
    size = layout.size
    scale = size / font.units_per_em
    steps = 3 if size < 16 else 5
    block = _block_height(size, len(layout.lines))
    baseline = y0 + (y1 - y0 - block) / 2 + 0.72 * size
    polygons = []
    for line in layout.lines:
        line_width = font.text_width(line, size) + tracking * max(0, len(line) - 1)
        pen = x0 + (x1 - x0 - line_width) / 2
        for ch in line:
            glyph = font.glyph_index(ord(ch))
            for contour in font.contours(glyph):
                flat = _flatten(contour, steps)
                if flat:
                    polygons.append([(pen + x * scale, baseline - y * scale) for x, y in flat])
            pen += font.advance(glyph) * scale + tracking
        baseline += 1.08 * size
    return _fill_polygons(polygons, width, height, 4)


# --------------------------------------------------------------------------
# PNG


def write_png(path: Path, width: int, height: int, rgba: bytes) -> None:
    raw = b"".join(b"\x00" + rgba[y * width * 4 : (y + 1) * width * 4] for y in range(height))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def read_png(path: Path) -> tuple[int, int, bytes]:
    """Decode a non-interlaced PNG to RGBA8."""

    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BuildError(f"Cannot read image {path}: {exc}") from exc
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise BuildError(f"{path} is not a PNG image.", hint="Convert the image to PNG.")
    pos = 8
    header = None
    palette = b""
    transparency = b""
    compressed = []
    while pos + 8 <= len(data):
        length, tag = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif tag == b"PLTE":
            palette = body
        elif tag == b"tRNS":
            transparency = body
        elif tag == b"IDAT":
            compressed.append(body)
        elif tag == b"IEND":
            break
    if header is None:
        raise BuildError(f"{path} has no PNG header.")
    width, height, depth, color_type, _compression, _filter, interlace = header
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None or depth not in (1, 2, 4, 8, 16) or (color_type in (2, 4, 6) and depth < 8):
        raise BuildError(f"{path}: unsupported PNG format (colour type {color_type}, depth {depth}).")
    if interlace:
        raise BuildError(f"{path} is an interlaced PNG.", hint="Save the image without interlacing.")
    if width * height > 4096 * 4096:
        raise BuildError(f"{path} is too large ({width}x{height}).")
    try:
        raw = zlib.decompress(b"".join(compressed))
    except zlib.error as exc:
        raise BuildError(f"{path}: corrupt PNG data ({exc}).") from exc

    bits = channels * depth
    stride = (width * bits + 7) // 8
    bpp = max(1, bits // 8)
    if len(raw) < height * (stride + 1):
        raise BuildError(f"{path}: truncated PNG data.")
    previous = bytearray(stride)
    rows = []
    pos = 0
    for _ in range(height):
        kind = raw[pos]
        line = bytearray(raw[pos + 1 : pos + 1 + stride])
        pos += 1 + stride
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = previous[i]
                c = previous[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predictor = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + predictor) & 0xFF
        elif kind != 0:
            raise BuildError(f"{path}: unknown PNG filter {kind}.")
        rows.append(line)
        previous = line

    out = bytearray(width * height * 4)
    maximum = (1 << depth) - 1
    key = struct.unpack(f">{len(transparency) // 2}H", transparency) if color_type in (0, 2) and transparency else None
    for y, line in enumerate(rows):
        if depth < 8:
            samples = [(line[i // (8 // depth)] >> (8 - depth - (i % (8 // depth)) * depth)) & maximum for i in range(width)]
        elif depth == 16:
            samples = [line[i] for i in range(0, len(line), 2)]
        else:
            samples = line
        for x in range(width):
            o = (y * width + x) * 4
            if color_type == 3:
                index = samples[x]
                rgb = palette[index * 3 : index * 3 + 3] or b"\x00\x00\x00"
                alpha = transparency[index] if index < len(transparency) else 255
                out[o : o + 4] = bytes((rgb[0], rgb[1], rgb[2], alpha))
                continue
            sample = samples[x * channels : x * channels + channels]
            if color_type in (0, 4):
                gray = sample[0] * 255 // maximum if depth < 8 else sample[0]
                alpha = sample[1] if color_type == 4 else 255
                if key and color_type == 0 and samples[x] == (key[0] >> (8 if depth == 16 else 0)):
                    alpha = 0
                out[o : o + 4] = bytes((gray, gray, gray, alpha))
            else:
                alpha = sample[3] if color_type == 6 else 255
                if key and color_type == 2 and len(key) == 3 and tuple(sample[:3]) == tuple(
                    k >> (8 if depth == 16 else 0) for k in key
                ):
                    alpha = 0
                out[o : o + 4] = bytes((sample[0], sample[1], sample[2], alpha))
    return width, height, bytes(out)


def _resize(width: int, height: int, rgba: bytes, crop: tuple[int, int, int, int], out_w: int, out_h: int) -> list[list[float]]:
    """Area-average resize of the crop box (premultiplied alpha)."""

    cx0, cy0, cx1, cy1 = crop
    cw, ch = cx1 - cx0, cy1 - cy0
    out = []
    for ty in range(out_h):
        sy0 = cy0 + ty * ch // out_h
        sy1 = max(sy0 + 1, cy0 + (ty + 1) * ch // out_h)
        for tx in range(out_w):
            sx0 = cx0 + tx * cw // out_w
            sx1 = max(sx0 + 1, cx0 + (tx + 1) * cw // out_w)
            r = g = b = a = 0.0
            n = 0
            for sy in range(sy0, sy1):
                base = (sy * width) * 4
                for sx in range(sx0, sx1):
                    o = base + sx * 4
                    alpha = rgba[o + 3] / 255.0
                    r += rgba[o] / 255.0 * alpha
                    g += rgba[o + 1] / 255.0 * alpha
                    b += rgba[o + 2] / 255.0 * alpha
                    a += alpha
                    n += 1
            if a > 0:
                out.append([r / a, g / a, b / a, a / n])
            else:
                out.append([0.0, 0.0, 0.0, 0.0])
    return out


# --------------------------------------------------------------------------
# Icon and banner pictures


def _game_color(seed: str) -> tuple[float, float, float]:
    index = hashlib.sha256(seed.encode("utf-8")).digest()[0] % len(_PALETTE)
    return tuple(c / 255.0 for c in _PALETTE[index])


def _shade(color, factor: float):
    if factor >= 1.0:
        return tuple(min(1.0, c + (1.0 - c) * (factor - 1.0)) for c in color)
    return tuple(c * factor for c in color)


def generated_icon(font: TrueTypeFont, title: str, seed: str) -> bytes:
    """48x48 opaque RGBA icon: the title on a gradient in an N64 colour."""

    size = ICON_SIZE
    color = _game_color(seed)
    canvas = Canvas(size, size, (0.0, 0.0, 0.0, 1.0))
    full = [1.0] * (size * size)
    canvas.fill_vertical_gradient(full, _shade(color, 1.25), _shade(color, 0.55), 0, size)
    # A one-pixel bevel: lighter top edge, darker frame.
    frame = [0.0] * (size * size)
    for i in range(size):
        for index in (i, (size - 1) * size + i, i * size, i * size + size - 1):
            frame[index] = 1.0
    canvas.fill(frame, (0.0, 0.0, 0.0), 0.35)
    canvas.fill([1.0 if index < size else 0.0 for index in range(size * size)], (1.0, 1.0, 1.0), 0.25)
    layout = _fit_text(font, title, size - 6, size - 6, 3, largest=20, smallest=9)
    text = _text_mask(font, layout, size, size, (3, 3, size - 3, size - 3))
    shadow = [0.0] * (size * size)
    for y in range(size - 1):
        for x in range(size - 1):
            shadow[(y + 1) * size + x + 1] = text[y * size + x]
    canvas.fill(shadow, (0.0, 0.0, 0.0), 0.55)
    canvas.fill(text, (1.0, 1.0, 1.0))
    return canvas.rgba_bytes(opaque_background=(0.0, 0.0, 0.0))


def generated_banner(font: TrueTypeFont, title: str, seed: str) -> bytes:
    """256x128 RGBA banner texture: a rounded card with the title."""

    width, height = BANNER_WIDTH, BANNER_HEIGHT
    color = _game_color(seed)
    canvas = Canvas(width, height)
    card = _rounded_rect_mask(width, height, 4, 6, width - 4, height - 6, 16)
    canvas.fill_vertical_gradient(card, _shade(color, 1.2), _shade(color, 0.5), 6, height - 6)
    rim = _rounded_rect_mask(width, height, 5.5, 7.5, width - 5.5, height - 7.5, 14.5)
    border = [max(0.0, outer - inner) for outer, inner in zip(card, rim)]
    canvas.fill(border, (1.0, 1.0, 1.0), 0.45)

    layout = _fit_text(font, title, width - 36, 70, 2, largest=44, smallest=14)
    text = _text_mask(font, layout, width, height, (18, 12, width - 18, 90))
    outline = _dilate(text, width, height, 2)
    canvas.fill(outline, (0.05, 0.05, 0.08), 0.75)
    canvas.fill(text, (1.0, 1.0, 1.0))

    subtitle = _Layout(12, ["NINTENDO 64"])
    sub = _text_mask(font, subtitle, width, height, (18, 92, width - 18, 112), tracking=2.0)
    canvas.fill(sub, (1.0, 1.0, 1.0), 0.85)
    return canvas.rgba_bytes()


def icon_from_image(path: Path) -> bytes:
    width, height, rgba = read_png(path)
    side = min(width, height)
    crop = ((width - side) // 2, (height - side) // 2, (width - side) // 2 + side, (height - side) // 2 + side)
    pixels = _resize(width, height, rgba, crop, ICON_SIZE, ICON_SIZE)
    canvas = Canvas(ICON_SIZE, ICON_SIZE)
    canvas.pixels = pixels
    return canvas.rgba_bytes(opaque_background=(0.0, 0.0, 0.0))


def banner_from_image(path: Path) -> bytes:
    width, height, rgba = read_png(path)
    scale = min(BANNER_WIDTH / width, BANNER_HEIGHT / height)
    out_w = max(1, min(BANNER_WIDTH, round(width * scale)))
    out_h = max(1, min(BANNER_HEIGHT, round(height * scale)))
    pixels = _resize(width, height, rgba, (0, 0, width, height), out_w, out_h)
    canvas = Canvas(BANNER_WIDTH, BANNER_HEIGHT)
    left = (BANNER_WIDTH - out_w) // 2
    top = (BANNER_HEIGHT - out_h) // 2
    for y in range(out_h):
        for x in range(out_w):
            canvas.pixels[(top + y) * BANNER_WIDTH + left + x] = pixels[y * out_w + x]
    return canvas.rgba_bytes()


# --------------------------------------------------------------------------
# LZ11 and the CBMD banner container


def lz11_decompress(source: bytes) -> bytes:
    if not source or source[0] != 0x11:
        raise ValueError("not LZ11 data")
    size = source[1] | (source[2] << 8) | (source[3] << 16)
    pos = 4
    if size == 0:
        size = struct.unpack_from("<I", source, 4)[0]
        pos = 8
    out = bytearray()
    while len(out) < size:
        flags = source[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= size:
                break
            if not flags & (0x80 >> bit):
                out.append(source[pos])
                pos += 1
                continue
            first = source[pos]
            indicator = first >> 4
            if indicator == 0:
                length = (((first & 0xF) << 4) | (source[pos + 1] >> 4)) + 0x11
                distance = (((source[pos + 1] & 0xF) << 8) | source[pos + 2]) + 1
                pos += 3
            elif indicator == 1:
                length = (((first & 0xF) << 12) | (source[pos + 1] << 4) | (source[pos + 2] >> 4)) + 0x111
                distance = (((source[pos + 2] & 0xF) << 8) | source[pos + 3]) + 1
                pos += 4
            else:
                length = indicator + 1
                distance = (((first & 0xF) << 8) | source[pos + 1]) + 1
                pos += 2
            if distance > len(out):
                raise ValueError("LZ11 back-reference before the start of the data")
            for _ in range(length):
                out.append(out[-distance])
    return bytes(out[:size])


def lz11_compress(data: bytes) -> bytes:
    size = len(data)
    if size >= 1 << 24:
        raise ValueError("LZ11 data too large")
    out = bytearray((0x11, size & 0xFF, (size >> 8) & 0xFF, (size >> 16) & 0xFF))
    if size == 0:
        out += bytes(4)  # a zero 24-bit size announces a 32-bit size field
    recent: dict[bytes, list[int]] = {}

    def remember(position: int) -> None:
        if position + 3 <= size:
            bucket = recent.setdefault(data[position : position + 3], [])
            bucket.append(position)
            if len(bucket) > 32:
                del bucket[:16]

    pos = 0
    while pos < size:
        flag_index = len(out)
        out.append(0)
        flags = 0
        for bit in range(8):
            if pos >= size:
                break
            best_length = 0
            best_distance = 0
            if pos + 3 <= size:
                limit = min(0x10110, size - pos)
                for candidate in reversed(recent.get(data[pos : pos + 3], ())):
                    distance = pos - candidate
                    if distance > 0x1000:
                        break
                    length = 3
                    while length < limit and data[candidate + length] == data[pos + length]:
                        length += 1
                    if length > best_length:
                        best_length, best_distance = length, distance
                        if length == limit:
                            break
            if best_length >= 3:
                d = best_distance - 1
                length = best_length
                if length <= 0x10:
                    out += bytes((((length - 1) << 4) | (d >> 8), d & 0xFF))
                elif length <= 0x110:
                    value = length - 0x11
                    out += bytes((value >> 4, ((value & 0xF) << 4) | (d >> 8), d & 0xFF))
                else:
                    value = length - 0x111
                    out += bytes((0x10 | (value >> 12), (value >> 4) & 0xFF, ((value & 0xF) << 4) | (d >> 8), d & 0xFF))
                flags |= 0x80 >> bit
                for position in range(pos, pos + length):
                    remember(position)
                pos += length
            else:
                out.append(data[pos])
                remember(pos)
                pos += 1
        out[flag_index] = flags
    return bytes(out)


_BAYER4 = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)


def _morton(x: int, y: int) -> int:
    value = 0
    for i in range(3):
        value |= ((x >> i) & 1) << (2 * i) | ((y >> i) & 1) << (2 * i + 1)
    return value


def encode_rgba4_tiled(width: int, height: int, rgba: bytes) -> bytes:
    """PICA200 RGBA4 texture in 8x8 Morton tiles, rows in the order the
    DaedalusX64 banner texture uses (top row first), ordered-dithered."""

    out = bytearray(width * height * 2)
    tiles_per_row = width // 8
    for y in range(height):
        for x in range(width):
            o = (y * width + x) * 4
            threshold = (_BAYER4[y & 3][x & 3] + 0.5) / 16.0
            r, g, b = (min(15, int(rgba[o + c] * 15 / 255 + threshold)) for c in range(3))
            a = (rgba[o + 3] * 15 + 127) // 255
            tile = (y // 8) * tiles_per_row + x // 8
            index = (tile * 64 + _morton(x & 7, y & 7)) * 2
            struct.pack_into("<H", out, index, (r << 12) | (g << 8) | (b << 4) | a)
    return bytes(out)


def _find_banner_texture(cgfx: bytes) -> int:
    """Offset of the 256x128 RGBA4 image data in the banner CGFX."""

    position = cgfx.find(b"TXOB")
    while position >= 0:
        try:
            kind = struct.unpack_from("<I", cgfx, position - 4)[0]
            height, width = struct.unpack_from("<II", cgfx, position + 0x14)
            pica_format = struct.unpack_from("<I", cgfx, position + 0x30)[0]
            data_size = struct.unpack_from("<I", cgfx, position + 0x40)[0]
            relative = struct.unpack_from("<i", cgfx, position + 0x44)[0]
        except struct.error:
            break
        if (
            kind == 0x20000011
            and (width, height) == (BANNER_WIDTH, BANNER_HEIGHT)
            and pica_format == 4
            and data_size == BANNER_WIDTH * BANNER_HEIGHT * 2
        ):
            data = position + 0x44 + relative
            if 0 < data and data + data_size <= len(cgfx):
                return data
        position = cgfx.find(b"TXOB", position + 4)
    raise BuildError(
        "The banner template has no 256x128 RGBA4 texture.",
        hint="The DaedalusX64 banner.bnr changed; update tools/n64to3ds/artwork.py.",
    )


def build_banner(template: bytes, texture_rgba: bytes) -> bytes:
    """The template CBMD banner with its texture replaced."""

    if template[:4] != b"CBMD":
        raise BuildError("The banner template is not a CBMD file.")
    offsets = struct.unpack_from("<14I", template, 0x08)
    audio = struct.unpack_from("<I", template, 0x84)[0]
    if offsets[0] != 0x88 or any(offsets[1:]) or not 0x88 < audio < len(template):
        raise BuildError("Unexpected banner template layout (several CGFX scenes or no audio).")
    cgfx = bytearray(lz11_decompress(template[0x88:audio]))
    data = _find_banner_texture(bytes(cgfx))
    cgfx[data : data + BANNER_WIDTH * BANNER_HEIGHT * 2] = encode_rgba4_tiled(BANNER_WIDTH, BANNER_HEIGHT, texture_rgba)
    compressed = lz11_compress(bytes(cgfx))
    header = bytearray(template[:0x88])
    audio_offset = (0x88 + len(compressed) + 0x1F) & ~0x1F
    struct.pack_into("<I", header, 0x84, audio_offset)
    padding = bytes(audio_offset - 0x88 - len(compressed))
    return bytes(header) + compressed + padding + template[audio:]


# --------------------------------------------------------------------------
# Entry point for the builder


@dataclass
class HomeArtwork:
    icon_png: Path
    banner: Path
    icon_source: str
    banner_source: str

    def as_dict(self) -> dict[str, str]:
        return {"icon": self.icon_source, "banner": self.banner_source}


def make_home_artwork(
    *,
    title: str,
    seed: str,
    font_path: Path,
    banner_template: Path,
    out_dir: Path,
    icon_image: Path | None = None,
    banner_image: Path | None = None,
) -> HomeArtwork:
    out_dir.mkdir(parents=True, exist_ok=True)
    font = None

    def get_font() -> TrueTypeFont:
        nonlocal font
        if font is None:
            font = TrueTypeFont.load(font_path)
        return font

    if icon_image is not None:
        icon = icon_from_image(icon_image)
        icon_source = f"image:{Path(icon_image).name}"
    else:
        icon = generated_icon(get_font(), title, seed)
        icon_source = "generated"
    icon_png = out_dir / "icon.png"
    write_png(icon_png, ICON_SIZE, ICON_SIZE, icon)

    if banner_image is not None:
        texture = banner_from_image(banner_image)
        banner_source = f"image:{Path(banner_image).name}"
    else:
        texture = generated_banner(get_font(), title, seed)
        banner_source = "generated"
    write_png(out_dir / "banner.png", BANNER_WIDTH, BANNER_HEIGHT, texture)
    banner = out_dir / "banner.bnr"
    banner.write_bytes(build_banner(Path(banner_template).read_bytes(), texture))
    return HomeArtwork(icon_png=icon_png, banner=banner, icon_source=icon_source, banner_source=banner_source)

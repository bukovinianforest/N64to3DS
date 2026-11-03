"""HOME Menu artwork: banner container, texture layout, LZ11, PNG and text fitting."""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.n64to3ds import artwork  # noqa: E402
from tools.n64to3ds.errors import BuildError  # noqa: E402

RESOURCES = ROOT / "third_party" / "daedalus" / "Source" / "SysCTR" / "Resources"
TEMPLATE = RESOURCES / "banner.bnr"
FONT = RESOURCES / "romfs" / "Roboto-Medium.ttf"

needs_daedalus = pytest.mark.skipif(not TEMPLATE.is_file() or not FONT.is_file(), reason="Daedalus tree not bootstrapped")


def _template_texture(template: bytes) -> tuple[bytes, int, bytes]:
    audio = struct.unpack_from("<I", template, 0x84)[0]
    cgfx = artwork.lz11_decompress(template[0x88:audio])
    offset = artwork._find_banner_texture(cgfx)
    return cgfx, offset, cgfx[offset : offset + 256 * 128 * 2]


def _decode_rgba4_tiled(data: bytes) -> bytes:
    rgba = bytearray(256 * 128 * 4)
    for y in range(128):
        for x in range(256):
            tile = (y // 8) * 32 + x // 8
            value = struct.unpack_from("<H", data, (tile * 64 + artwork._morton(x & 7, y & 7)) * 2)[0]
            o = (y * 256 + x) * 4
            rgba[o : o + 4] = bytes(((value >> s) & 15) * 17 for s in (12, 8, 4, 0))
    return bytes(rgba)


def test_lz11_round_trip_with_long_runs_and_short_matches():
    data = bytes(range(256)) * 40 + b"\0" * 70000 + b"abcabcabd" * 500 + bytes(range(5, 250, 3)) * 30
    packed = artwork.lz11_compress(data)
    assert packed[0] == 0x11
    assert len(packed) < len(data) // 4
    assert artwork.lz11_decompress(packed) == data


def test_lz11_round_trip_tiny_inputs():
    for data in (b"", b"a", b"ab", b"abc", b"aaaa"):
        assert artwork.lz11_decompress(artwork.lz11_compress(data)) == data


@needs_daedalus
def test_template_texture_round_trips_bit_exactly():
    template = TEMPLATE.read_bytes()
    _cgfx, _offset, texture = _template_texture(template)
    rgba = _decode_rgba4_tiled(texture)
    assert artwork.encode_rgba4_tiled(256, 128, rgba) == texture


@needs_daedalus
def test_rebuilt_banner_keeps_scene_and_audio_and_replaces_texture():
    template = TEMPLATE.read_bytes()
    cgfx, offset, texture = _template_texture(template)
    identical = artwork.build_banner(template, _decode_rgba4_tiled(texture))
    audio = struct.unpack_from("<I", identical, 0x84)[0]
    assert audio % 0x20 == 0
    assert artwork.lz11_decompress(identical[0x88:audio]) == cgfx
    assert identical[audio:] == template[struct.unpack_from("<I", template, 0x84)[0] :]

    red = bytes((255, 0, 0, 255)) * (256 * 128)
    banner = artwork.build_banner(template, red)
    new_audio = struct.unpack_from("<I", banner, 0x84)[0]
    new_cgfx = artwork.lz11_decompress(banner[0x88:new_audio])
    assert new_cgfx[:offset] == cgfx[:offset]
    assert new_cgfx[offset + len(texture) :] == cgfx[offset + len(texture) :]
    assert set(struct.unpack_from(f"<{256 * 128}H", new_cgfx, offset)) == {0xF00F}


def test_build_banner_rejects_other_files():
    with pytest.raises(BuildError):
        artwork.build_banner(b"NOPE" + bytes(0x200), bytes(256 * 128 * 4))


def _png(width: int, height: int, color_type: int, rows: list[bytes], *, depth: int = 8, extra: bytes = b"") -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    raw = b"".join(rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color_type, 0, 0, 0))
        + extra
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_png_round_trip(tmp_path):
    pixels = b"".join(bytes((x * 40, y * 60, 200, 255 - x * 20)) for y in range(3) for x in range(5))
    path = tmp_path / "a.png"
    artwork.write_png(path, 5, 3, pixels)
    assert artwork.read_png(path) == (5, 3, pixels)


def test_png_filters_palette_and_gray(tmp_path):
    # RGB with Sub, Up, Average and Paeth filters on rows of a 3x4 image.
    rgb = [bytes((10 * x + y, 20 * y, 7 * x * y)) for y in range(4) for x in range(3)]
    expected = b"".join(p + b"\xff" for p in rgb)

    def filtered(kind: int, row: bytes, previous: bytes) -> bytes:
        out = bytearray()
        for i, value in enumerate(row):
            a = row[i - 3] if i >= 3 else 0
            b = previous[i]
            c = previous[i - 3] if i >= 3 else 0
            if kind == 1:
                pred = a
            elif kind == 2:
                pred = b
            elif kind == 3:
                pred = (a + b) >> 1
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            out.append((value - pred) & 0xFF)
        return bytes((kind,)) + bytes(out)

    rows = []
    previous = bytes(9)
    for y, kind in enumerate((1, 2, 3, 4)):
        row = b"".join(rgb[y * 3 : y * 3 + 3])
        rows.append(filtered(kind, row, previous))
        previous = row
    path = tmp_path / "rgb.png"
    path.write_bytes(_png(3, 4, 2, rows))
    assert artwork.read_png(path) == (3, 4, expected)

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    palette = chunk(b"PLTE", bytes((255, 0, 0, 0, 255, 0))) + chunk(b"tRNS", bytes((255, 0)))
    path.write_bytes(_png(4, 1, 3, [b"\x00" + bytes((0b01000100,))], depth=2, extra=palette))
    # 2-bit indices 1, 0, 1, 0: green (transparent via tRNS), red, green, red.
    assert artwork.read_png(path) == (4, 1, bytes((0, 255, 0, 0, 255, 0, 0, 255, 0, 255, 0, 0, 255, 0, 0, 255)))

    path.write_bytes(_png(2, 1, 0, [b"\x00" + bytes((0, 255))]))
    assert artwork.read_png(path)[2] == bytes((0, 0, 0, 255, 255, 255, 255, 255))


def test_png_rejects_interlaced(tmp_path):
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    path = tmp_path / "i.png"
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 1)) + chunk(b"IEND", b"")
    )
    with pytest.raises(BuildError):
        artwork.read_png(path)


def test_short_titles_and_abbreviation():
    assert artwork._short_titles("The Legend of Zelda: Ocarina of Time") == [
        "The Legend of Zelda: Ocarina of Time",
        "Legend of Zelda: Ocarina of Time",
        "Ocarina of Time",
        "Legend of Zelda",
    ]
    assert artwork._abbreviation("GoldenEye 007") == "GE007"
    assert artwork._split_camel("GoldenEye") == ["Golden", "Eye"]


@needs_daedalus
def test_generated_icon_and_banner(tmp_path):
    font = artwork.TrueTypeFont.load(FONT)
    icon = artwork.generated_icon(font, "Harvest Moon 64", "seed")
    assert len(icon) == 48 * 48 * 4
    assert all(icon[i + 3] == 255 for i in range(0, len(icon), 4))
    # White text was drawn: some pixels are near white.
    assert any(min(icon[i : i + 3]) > 230 for i in range(0, len(icon), 4))

    banner = artwork.generated_banner(font, "The Legend of Zelda: Ocarina of Time", "seed")
    assert len(banner) == 256 * 128 * 4
    assert banner[3] == 0  # transparent outside the card
    assert banner[(64 * 256 + 128) * 4 + 3] == 255

    result = artwork.make_home_artwork(
        title="GoldenEye 007",
        seed="abc",
        font_path=FONT,
        banner_template=TEMPLATE,
        out_dir=tmp_path / "art",
    )
    assert result.icon_png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert result.banner.read_bytes()[:4] == b"CBMD"
    assert result.as_dict() == {"icon": "generated", "banner": "generated"}


@needs_daedalus
def test_user_images(tmp_path):
    wide = b"".join(bytes((x, 255 - x, 0, 255)) for _y in range(50) for x in range(200))
    source = tmp_path / "cover.png"
    artwork.write_png(source, 200, 50, wide)
    result = artwork.make_home_artwork(
        title="X",
        seed="abc",
        font_path=FONT,
        banner_template=TEMPLATE,
        out_dir=tmp_path / "art",
        icon_image=source,
        banner_image=source,
    )
    assert result.as_dict() == {"icon": "image:cover.png", "banner": "image:cover.png"}
    _w, _h, icon = artwork.read_png(result.icon_png)
    assert (_w, _h) == (48, 48)
    banner = artwork.read_png(tmp_path / "art" / "banner.png")[2]
    # 200x50 fitted into 256x128 is 256x64, centred: transparent above, opaque in the middle.
    assert banner[(10 * 256 + 100) * 4 + 3] == 0
    assert banner[(64 * 256 + 100) * 4 + 3] == 255

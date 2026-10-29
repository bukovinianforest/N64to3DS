# Third-party notices

This file records the third-party components used by n64to3ds, their pinned
versions or commits, and their licenses.  It is not a substitute for the full
license texts distributed with those components.

## DaedalusX64

- Project: https://github.com/DaedalusX64/daedalus
- Pinned commit: `580c0c409358db332149fc2903c610449434f8fc`
- License: GNU General Public License version 2
- Use: 3DS emulator runtime (CPU, Dynarec, interpreter, RSP, RDP, audio,
  input, saves, timing, 3DS backend)
- Local changes: `patches/daedalus-n64to3ds.patch` adds the standalone RomFS
  autoboot path, AOT dispatch hooks, cache/DMA invalidation hooks, runtime
  diagnostics, atomic save replacement, and the CMake packaging overrides.
  No game logic is patched.

The full GPL-2.0 text is in `LICENSE`.  DaedalusX64's own `copying.txt` remains
in the upstream tree when it is cloned by `scripts/bootstrap.sh`.

## Rabbitizer

- Project: https://github.com/Decompollaborate/rabbitizer
- Pinned version: `1.16.2`
- License: MIT
- Use: MIPS instruction decoding/validation on the host.  The analyzer still
  applies its own control-flow and supported-subset checks; Rabbitizer is not
  used as proof that a byte sequence is code.

## ipl3checksum

- Project: https://github.com/Decompollaborate/ipl3checksum
- Pinned version: `1.3.1`
- License: MIT
- Use: optional IPL3 CIC detection and header CRC comparison.  A mismatch is a
  warning, not a build failure, because homebrew and patched ROMs are common.

## devkitPro / devkitARM / libctru / picaGL

- Project: https://devkitpro.org/
- Use: ARM11 cross-compiler, libctru, picaGL, `makerom`, `3dsxtool`,
  `mkromfs3ds`, `smdhtool`
- License: components have their own licenses (typically zlib/libpng-style for
  libctru and picaGL; see the devkitPro packages and upstream projects).
- The repository does not vendor devkitPro when it is installed system-wide.
  `scripts/bootstrap.sh --from-image` extracts the pinned Daedalus image
  locally; it does not redistribute the image contents.

## Python standard library

The CLI and tests use only the Python standard library in addition to the
packages above.  Python is distributed under the PSF license.

## Commercial ROMs and firmware

No commercial ROM, BIOS, firmware, encryption key, title key, or proprietary
game asset is included in this repository or in the generated build artifacts
except the user-supplied ROM that the user explicitly asks the tool to embed.

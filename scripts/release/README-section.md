## Prebuilt releases (no toolchain install)

The [Releases](../../releases) page has self-contained archives for Linux and
Windows (x86_64).  Each one ships Python, devkitARM r66 with libctru/picaGL,
3dstools, makerom and the patched Daedalus sources, so you do not need
devkitPro, CMake or Python.  The CLI is the same as in a source checkout;
every build still compiles Daedalus for your ROM, which takes a few minutes.

| Archive | Host |
| --- | --- |
| `n64to3ds-<version>-linux-x86_64.tar.xz` | Linux, glibc 2.34+ (Ubuntu 22.04+, Debian 12+, Fedora 35+) |
| `n64to3ds-<version>-windows-x86_64.zip` | Windows 10/11 64-bit; extract to `C:\` or another short ASCII-only path |

Harvest Moon 64 example on Linux:

```bash
tar -xJf n64to3ds-<version>-linux-x86_64.tar.xz
cd n64to3ds-<version>-linux-x86_64
./n64to3ds build "/path/to/Harvest Moon 64 (USA).z64" --output HM64.cia
```

On Windows:

```bat
cd C:\n64to3ds-0.2.0-windows-x86_64
.\n64to3ds.cmd build "D:\roms\Harvest Moon 64 (USA).z64" --output HM64.cia
```

The bundle cannot use devkitPro's CMake toolchain files on Windows (they
require msys2), so it replays a recorded Daedalus build instead
(`tools/n64to3ds/kit.py`).  `scripts/make_release.py` records the compile and
link commands from CMake and checks that, in the same directory, both builds
give a byte-identical ELF, 3DSX and CIA content for every variant.  The AOT
host semantic self-test needs a native `g++`, so it runs only on Linux hosts
that have one.

Maintainers build the archives on Linux from a checkout bootstrapped with
`scripts/bootstrap.sh --from-image`:

```bash
python3 scripts/make_release.py --check-rom "/path/to/Harvest Moon 64 (USA).z64"
```

The archives and `SHA256SUMS.txt` are written to `out/release/`.

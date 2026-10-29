Self-contained n64to3ds builds for Linux and Windows (x86_64).  Download one
archive, extract it, and run `n64to3ds build`.  No devkitPro, CMake, make or
Python install is needed.

**Included:** n64to3ds, CPython 3.12.11, devkitARM r66 (GCC 15.1.0), libctru,
picaGL, 3dstools 1.3.1, makerom 0.18.4 and the patched DaedalusX64 sources
(pinned commit `580c0c4` + `patches/daedalus-n64to3ds.patch`).  No ROMs,
keys or firmware.

### Requirements

- **Linux:** glibc 2.34+ (Ubuntu 22.04+, Debian 12+, Fedora 35+).  Optional
  `g++` for the AOT semantic self-test.
- **Windows:** Windows 10/11 64-bit.  Extract the zip to `C:\` (or another
  short ASCII-only path).  The AOT semantic self-test is skipped.
- About 0.5 GB (Windows) / 0.7 GB (Linux) unpacked, plus about 200 MB
  temporary space per build.  Every build compiles Daedalus for the ROM,
  which takes a few minutes.

### Harvest Moon 64 example

```bash
tar -xJf n64to3ds-0.1.0-linux-x86_64.tar.xz
cd n64to3ds-0.1.0-linux-x86_64
./n64to3ds build "/path/to/Harvest Moon 64 (USA).z64" --output HM64.cia
```

```bat
cd C:\n64to3ds-0.1.0-windows-x86_64
.\n64to3ds.cmd build "D:\roms\Harvest Moon 64 (USA).z64" --output HM64.cia
```

Install `HM64.cia` with FBI.  The Harvest Moon 64 (USA) build gets the
bottom-screen inventory/status extension automatically; `--extensions off`
disables it.  See `README.txt` in the archive for all options.

### What was checked for this release

@CHECKS@

A built CIA is not proof that a game is playable or runs at full speed on a
real Old 3DS.

### Licenses

n64to3ds and DaedalusX64 are GPL; their sources are inside the archive and in
this repository.  The bundled devkitPro, makerom, CPython, rabbitizer and
ipl3checksum binaries are unmodified upstream releases; licenses are in the
archive.  devkitPro does not support this bundle, so please report problems
here.

`SHA256SUMS.txt` lists the archive checksums.

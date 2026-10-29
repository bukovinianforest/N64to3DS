n64to3ds release bundle
=======================

n64to3ds packages an N64 ROM you own into a standalone Old 3DS/2DS CIA with
the DaedalusX64 runtime.  This archive is self-contained: it includes Python,
devkitARM r66 with libctru/picaGL, 3dstools and makerom, and the patched
Daedalus sources.  You do not need devkitPro, CMake, make or Python installed.
Each build compiles Daedalus for your ROM, which takes a few minutes.

No ROMs, keys or firmware are included.  Use only ROMs you have dumped
yourself.


Requirements
------------

Linux:   x86_64, glibc 2.34 or newer (Ubuntu 22.04+, Debian 12+, Fedora 35+),
         libstdc++ (present on any desktop install).  Optional: g++ for the
         AOT semantic self-test.
Windows: Windows 10 or 11, 64-bit.  Extract the zip to C:\ (or another short path
         with only ASCII characters).  The AOT semantic self-test is
         skipped on Windows (it needs a native g++).

About 1.5 GB of free disk space after extraction.


Example: Harvest Moon 64
------------------------

Linux:

    tar -xJf n64to3ds-<version>-linux-x86_64.tar.xz
    cd n64to3ds-<version>-linux-x86_64
    ./n64to3ds doctor
    ./n64to3ds build "/path/to/Harvest Moon 64 (USA).z64" --output "HM64.cia"

Windows (Command Prompt or PowerShell):

    cd C:\n64to3ds-0.1.0-windows-x86_64
    .\n64to3ds.cmd doctor
    .\n64to3ds.cmd build "D:\roms\Harvest Moon 64 (USA).z64" --output HM64.cia

The ROM may be .z64, .v64 or .n64.  For Harvest Moon 64 (USA) the build
automatically adds the bottom-screen inventory/status extension; other ROMs
get the stock Daedalus bottom screen.

Useful options:

    --aot off             build without ahead-of-time compiled blocks
    --extensions off      no bottom-screen extension
    --app-name "Name"     HOME menu name
    --title-id ID         explicit title id (16 hex digits)
    --verbose             show every compiler command

Check a finished CIA against its ROM:

    ./n64to3ds verify HM64.cia --rom "/path/to/Harvest Moon 64 (USA).z64"


Output
------

    HM64.cia                 install on the console (for example with FBI)
    HM64.3dsx                the same runtime for the Homebrew Launcher/emulators
    HM64.cia.manifest.json   build and verification record
    HM64.cia.build.log       full build log
    HM64.cia.aot.cpp/.json   generated AOT program (with --aot auto)

Saves and save states are written to sdmc:/3ds/DaedalusX64/.

"CIA built" and "verify PASS" are structural checks only.  They do not prove
that a game boots, is playable or runs at full speed on a real Old 3DS.


Licenses and sources
--------------------

n64to3ds and DaedalusX64 are GPL-2.0-or-later / GPL-2.0 (see LICENSE, NOTICE,
THIRD_PARTY_NOTICES.md).  The Daedalus sources used by the build are in
third_party/daedalus (pinned upstream commit plus
patches/daedalus-n64to3ds.patch).  Bundled third-party binaries, unmodified:

  devkitARM r66, libctru, picaGL, 3dstools   https://devkitpro.org
      (licenses in third_party/devkitpro/licenses; toolchain sources:
      https://github.com/devkitPro/buildscripts)
  makerom v0.18.4 (MIT)                       https://github.com/3DSGuy/Project_CTR
  CPython 3.12.11 (PSF)                       https://github.com/astral-sh/python-build-standalone
  rabbitizer 1.16.2, ipl3checksum 1.3.1 (MIT) https://pypi.org

devkitPro does not support this bundle.  Please report problems with it to
n64to3ds, not to devkitPro.

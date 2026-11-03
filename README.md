# n64to3ds

`n64to3ds` packages a byte-order-normalized Nintendo 64 ROM into a standalone,
installable Old 3DS/2DS CIA.  The runtime is a minimally patched
[DaedalusX64](https://github.com/DaedalusX64/daedalus) 3DS build with an optional
ahead-of-time (AOT) compiler for statically proven MIPS blocks.  The ROM is
embedded in RomFS and opened automatically at startup; there is no ROM browser,
external ROM file, or game-specific port.

This project is **not** a Harvest Moon 64 port.  Harvest Moon 64 (USA) is the
commercial ROM used for end-to-end validation, but the build pipeline is
game-agnostic.

## Status

The status labels are deliberately independent.  A packaged CIA does not imply
that the emulator booted, that AOT code ran, that a game is playable, or that it
runs at full speed.

| Status | Result for the validated Harvest Moon 64 (USA) build |
| --- | --- |
| CIA built | **Yes**. Both `--aot auto` and `--aot off` CIAs, plus three synthetic-ROM CIAs, were produced and structurally verified. |
| emulator boot verified | **Yes, under Azahar, `--aot auto` only — boot path, not visible rendering**. The recorded run wrote `n64to3ds first frame pc=0x80105e60 title=000400000D217700` and executed 848,399 guest instructions, but the window showed a black screen and no later end-of-frame callback or AOT heartbeat was logged. This is not a playability claim. The `--aot off` baseline did not reach the end-of-frame callback within 10 minutes in the same emulator, so its boot status remains unverified. |
| AOT exercised | **Yes**. Generated blocks executed in the real 3DS runtime: 141,456 block executions and 848,399 guest instructions were recorded by the first end-of-frame callback, with 64+ distinct blocks. |
| game playable | **No / not proven**. With the current patch, Azahar (Old 3DS mode) renders the HM64 intro and attract-mode scenes at about 25-27 FPS. No input or gameplay session was tested. |
| full speed | **No / not proven**. |
| hardware verified | **No**. No physical Old 3DS/2DS was available. |
| hardware-unverified | **Yes**. This must be reported for every build made here. |

The recorded evidence was written into the auto manifest with
`record-runtime`.  It sets only `emulator boot verified` and `AOT exercised`;
playability, full speed, and hardware verification remain false.  The boot
evidence is the runtime's end-of-frame callback, which does not inspect pixels.
That recorded run used an older runtime that crashed under Azahar right after the
callback; see the Limitations section.  The evidence logs are copied next to the build output and are not
committed to this repository.

The host-side semantic harness separately executes every generated block against
an independent MIPS reference interpreter.  That proves generator/decoder
agreement, but it is not a substitute for on-device execution.

## Requirements

Tested on Ubuntu 22.04 with:

- Python 3.10+
- CMake 3.22+
- A host C++17 compiler (`g++`) for the AOT semantic self-test
- Git
- devkitPro with devkitARM, libctru, picaGL, `makerom`, `3dsxtool`,
  `mkromfs3ds`, and `smdhtool`
- The pinned DaedalusX64 commit `580c0c409358db332149fc2903c610449434f8fc`

Python dependencies are pinned in `requirements.txt`:

```
ipl3checksum==1.3.1
rabbitizer==1.16.2
```

Rabbitizer is used only as the proven MIPS decoder/validator.  The AOT analyzer
does not trust it blindly: it still applies its own control-flow and supported-
instruction checks, and anything ambiguous stays on Daedalus.

## Clean Ubuntu setup

Install the base packages:

```bash
sudo apt update
sudo apt install -y build-essential cmake git python3 python3-pip
```

Install devkitPro using the official pacman repository.  Follow the devkitPro
instructions for your Ubuntu release, then install the 3DS packages:

```bash
sudo dkp-pacman -Syu
sudo dkp-pacman -S 3ds-dev
```

Ensure `DEVKITPRO` is set (normally `/opt/devkitpro`) and that
`$DEVKITPRO/tools/bin/makerom`, `3dsxtool`, `mkromfs3ds`, and `smdhtool` exist.

Clone this repository, then run:

```bash
./scripts/bootstrap.sh
```

The script installs the pinned Python packages, clones Daedalus at the pinned
commit, and applies `patches/daedalus-n64to3ds.patch`.  If you cannot install
devkitPro system-wide, the pinned Daedalus Docker image can be used instead
(Docker or Podman required):

```bash
./scripts/bootstrap.sh --from-image
```

That extracts devkitPro and `makerom` into `third_party/devkitpro-root` and
creates the `third_party/devkitpro` symlink.  No sudo is required for this path.

`scripts/build.sh` is a thin wrapper that sets `DEVKITPRO`, `DEVKITARM`, and
`PATH` for either layout.

## Build

```bash
./scripts/build.sh build "Harvest Moon 64 (USA).z64" \
    --target old3ds \
    --output "Harvest Moon 64.cia"
```

Supported input byte orders are `.z64`, `.v64`, and `.n64`.  The ROM is
normalized to z64 before analysis and embedding.  Spaces in paths are fine.

Common options:

```bash
# Disable AOT entirely; Daedalus Dynarec/interpreter handles all code.
./scripts/build.sh build game.z64 --aot off --output game.cia

# Keep the intermediate build directory and show tool output.
./scripts/build.sh build game.z64 --keep-build --build-dir build/game --verbose

# Override the derived title id.
./scripts/build.sh build game.z64 --title-id 000400000D217700 --output game.cia

# Use a custom Daedalus roms.ini database.
./scripts/build.sh build game.z64 --rom-db /path/to/roms.ini
```

Every successful build writes these artifacts next to the CIA:

- `<output>.cia` — installable Old 3DS CIA
- `<output>.3dsx` — the same runtime for emulator testing
- `<output>.cia.manifest.json` — factual build and verification record
- `<output>.cia.build.log` — full toolchain log
- `<output>.cia.aot.cpp` and `<output>.cia.aot.json` — generated AOT program
  and metadata (when `--aot auto`)

The CIA is verified structurally after `makerom`: the tool parses the
CIA/NCCH/RomFS structures, validates the IVFC hashes, and compares the embedded
ROM bytes with the normalized input.  The ROM is stored uncompressed so this
byte-for-byte check is meaningful.

## Game-agnostic validation

The pipeline was exercised end-to-end with a synthetic 128 KiB homebrew-style
ROM in all three byte orders (`.z64`, `.v64`, `.n64`).  All three inputs
normalized to the same SHA-256 and produced structurally valid CIAs.  The
`--aot auto` build used an explicit `--title-id 000400000ABCDE00` and passed the
host semantic harness; the `.v64` and `.n64` builds used `--aot off` and the
derived title id.  This proves that the build path does not depend on Harvest
Moon 64.  The synthetic image is a pipeline fixture, not a playable game, and no
emulator-boot or AOT-execution claim is made for it.

## Runtime evidence

Build-time verification cannot prove that a CIA boots.  After running the
`.3dsx` or installed CIA in an emulator or on hardware, copy the logs from
`sdmc:/3ds/DaedalusX64/` and record them:

```bash
./scripts/build.sh record-runtime "Harvest Moon 64.cia.manifest.json" \
    --aot-log n64to3ds-aot.log \
    --daedalus-log daedalus.log
```

`record-runtime` updates only the claims the logs can support:

- `emulator boot verified` becomes yes only if the runtime's first end-of-frame
  callback is found.  That callback does not inspect pixels.  New builds print
  the title id on that line; a mismatched title id is an error.
- `AOT exercised` becomes yes only if a generated block executed and the AOT
  log's `rom_sha256` matches the manifest.  A mode mismatch, ROM mismatch, or
  title-id mismatch is an error and the manifest is left unchanged.
- `game playable` and `full speed` are never inferred.  They stay no unless a
  future hardware workflow explicitly establishes them.
- `hardware-unverified` remains yes unless physical hardware verification is
  recorded separately.

The evidence logs are copied next to the manifest by default, and their
SHA-256 hashes are recorded in the manifest.

## How the AOT path works

The host-side analyzer starts only from architectural entry points:

- the standard IPL3 load address `0x80000400`,
- the header entry point,
- and a small set of scored mapping hypotheses.

It follows direct branches and jumps, includes delay slots, and resolves only
local constant `jr`/`jalr` targets (for example the `lui`/`addiu` sequence in a
custom boot stub).  It never performs a linear sweep over the ROM and never
treats data as code.

The supported subset is intentionally conservative:

- 32/64-bit integer ALU and shifts
- 32-bit `mult`/`multu`/`div`/`divu` and HI/LO moves
- aligned integer loads and stores (`lb`, `lbu`, `lh`, `lhu`, `lw`, `lwu`,
  `ld`, `sb`, `sh`, `sw`, `sd`)
- direct and conditional branches, `j`, `jal`, `jr`, `jalr`, with delay slots

CP0, FPU, cache/TLB operations, traps, unaligned loads/stores, and
branch-likely instructions are left to Daedalus.  If a block cannot be proven
safe, the runtime falls back to the normal Daedalus Dynarec/interpreter path.

Because a custom IPL3 can load ROM code at an address that differs from the
standard mapping, the builder may emit blocks for more than one mapping
hypothesis.  Each descriptor records its `mapping_base`, `source_offset`, and a
fingerprint of the exact instruction words.  At runtime the dispatcher tries
every descriptor at the current PC and executes only the one whose fingerprint
matches the bytes actually resident in RDRAM.  A wrong hypothesis is harmless:
its blocks never validate.

Code identity is checked again whenever Daedalus reports an I-cache
invalidation, a PI DMA, or a `Memory_Reset`.  A generation counter forces
re-fingerprinting before any block can run again.  The AOT blocks use the same
authoritative `gGPR`/`gCPUState` as Daedalus; there is no second CPU context and
no per-transition context copy.

The runtime also guards scheduled events: a block runs only when the first
event is at least as far away as the whole block, and Daedalus' counter is
updated in bulk afterwards.  Memory operations stop at the same instruction
boundary the interpreter would use if a TLB exception or other job is queued.

## CLI reference

```
n64to3ds build <rom> [--target old3ds] [--output <cia>] [--aot off|auto]
                    [--title-id <id>] [--app-name <name>] [--icon <png>]
                    [--banner-image <png>] [--rom-db <ini>] [--save-type <type>]
                    [--jobs N] [--keep-build] [--build-dir <dir>] [--verbose]
n64to3ds analyze <rom> [--json] [--output <json>] [--hypotheses N]
                       [--max-blocks N] [--max-instructions N]
n64to3ds verify <cia> [--rom <rom>] [--title-id <id>] [--json]
n64to3ds record-runtime <manifest> [--aot-log <log>] [--daedalus-log <log>]
n64to3ds doctor [--verbose]
n64to3ds version
```

`analyze` never executes the ROM.  It reports the chosen mapping, candidate
scores, safe blocks, instruction counts, and unsupported instruction counts.

`doctor` checks the host toolchain, the pinned Daedalus tree, and the runtime
sources.

### HOME Menu name, icon and banner

The HOME Menu shows the game's name from the Daedalus ROM database
("GoldenEye 007"), or the ROM header name for ROMs it does not know;
`--app-name` overrides it.  The 48x48 icon and the top-screen banner are drawn
with that name on a card in one of the Nintendo 64 logo colours, chosen from
the ROM hash.  `--icon` and `--banner-image` take PNG images instead (for
example box art you own): the icon is cropped to a square and scaled to 48x48,
the banner image is fitted into 256x128 with its transparency kept.  The
banner keeps DaedalusX64's banner model and jingle and replaces only its
texture.  Everything is drawn in Python with the Roboto font Daedalus already
ships, so the release bundle needs no imaging library.  The HOME Menu may keep
showing an old icon for a reinstalled title until the console restarts.

## Title IDs

Application title IDs use the form `00040000_0UUUUU00`.  The 20-bit unique ID
is derived from the normalized ROM's SHA-256 with a domain separator, so the
same ROM gets a stable ID.  A local registry under
`$XDG_DATA_HOME/n64to3ds/titles.json` (or `~/.local/share/n64to3ds/`) prevents
silent collisions between different ROMs on the same machine.  Use
`--title-id` to override it explicitly; an explicit ID already registered to a
different ROM is rejected unless you remove the registry entry.

The RomFS ROM filename is `<title-id>.z64`.  Daedalus uses the ROM filename for
its save file, so this also gives each embedded game a distinct save name.

## Old 3DS targeting

The build is intentionally Old 3DS/2DS-safe:

- New 3DS-only RSF fields (`SystemModeExt`, 804 MHz CPU, L2 cache, core 2) are
  removed.
- `osSetSpeedupEnable(true)` is not called.
- The runtime uses only the ARM11 ARMv6K feature set.
- The CIA is built with `EnableCompress: false` so the embedded ROM can be
  verified byte-for-byte.

## Timing and audio on Old 3DS

The runtime paces the guest's VI interrupt in real time: VIs come every TV
frame (1/60 s NTSC) whatever the emulation speed, and the number of guest
cycles per VI adapts to what the console can execute (20-100 % of the N64's).
The guest therefore sees a slower CPU, drops frames of its own accord and keeps
its audio and retrace-driven logic at full rate, as an N64 with a slower CPU
would.  The audio ring trims that pace by up to 8 % so that the guest's
per-retrace audio production settles on the DSP's consumption; samples keep
their pitch.  Two more pieces make NuSys-style audio managers run every
retrace: a graphics task rendering on core 1 is reported as *yielded* when the
guest asks (libultra restarts it afterwards; nothing is drawn twice), and the
AI FIFO model queues three buffers instead of the hardware's one, which is safe
because an AI_LEN write copies the samples out at once.  Measured on an Old
3DS in HM64's attract scenes: 746 to 10 ring underruns per minute, 54 to 59.5
VI/s, 25.4 to 22.8 presents/s (the audio HLE now runs ~60 instead of ~45 times
a second next to the display list on core 1).  A `n64to3ds diag:` line in
`sdmc:/3ds/DaedalusX64/daedalus.log` about every 10 s of real time records
these counters; `scripts/triage_log.py` reads it.

Other general fixes, found with the console profiler on GoldenEye 007:

- `TLBWR` wrote every random TLB refill into the same entry (the index was a
  `static` computed once), so games that refill the TLB from their miss
  handler thrashed: GoldenEye took ~12000 TLB misses a second and ran at ~1%
  of N64 speed behind a black screen.  The index now follows the R4300's
  Random register (counting down from 31 to Wired).
- TLB-mapped accesses go through a 4 KB page translation cache.
- The AOT dispatcher rejects PCs that start no AOT block with a bitmap before
  its binary search; it runs before every interpreted instruction.
- ROMs of 8 MB or more were streamed from RomFS in 2 KB chunks on Old 3DS
  (~15 ms per miss).  They are now read into linear memory once when it has
  room to spare (the boot takes 1-2 s longer).
- The ARM dynarec compiles `div`/`divu` (through a helper that leaves the
  cached registers alone) and treats data-cache `cache` operations as the
  no-ops they are in the interpreter, instead of calling the interpreter.
- HOME and the power button are also handled once a second from the VI
  interrupt, so a game that renders rarely can still be closed.

ARM dynarec code generation (measured with `guest_mips`, the guest
instructions executed per second, in GoldenEye's Dam mission; a code dump of
the hottest fragments showed ~3 ARM instructions per MIPS instruction in
fragment bodies but a fragment entered every ~15 guest instructions, so most
of the time went to fragment transitions and memory stalls):

- A branch-likely that the recorded trace did not take left the fragment
  into its delay slot, so every iteration of a loop ending in such a branch
  ran one instruction in the interpreter and re-entered the dynarec (10% of
  core 0 in GoldenEye).  The exit now runs the delay slot natively and jumps
  to the branch target, which links to its fragment.
- Indirect exits (mostly `jr ra` returning to another caller than the one
  the trace recorded, ~140k a second in GoldenEye) update the counters
  inline and find their target in a 1024-entry table instead of two C calls
  with a spin lock.
- Direct exits update COUNT and the event counter inline instead of calling
  a stub.
- Plain arithmetic, loads and stores in delay slots no longer store the
  `Delay` flag around them (loads and stores set it in their slow path).
- The high word of a 32-bit result is stored when the register is flushed
  instead of after every write.

Together: GoldenEye Dam 9.1-9.9 -> 12.4-15 guest MIPS, Harvest Moon 64
attract scenes 17.4-18.2 -> 20.7-25.5.

New code and the hot-trace bookkeeping (GoldenEye frames that compile 100-400
new fragments took 150-550 ms):

- A trace cut short by an interrupt or exception was dropped, but the hot
  count of its start address had already reached the threshold, and tracing
  starts only when the count equals it, so that code stayed interpreted until
  the hot-trace map filled up.  Which code that hit depended on interrupt
  timing: in some GoldenEye runs the interpreter executed 110-180k
  instructions a second instead of 9-20k from the title screen on.  The count
  now starts again (`trace_aborts` in the diag line: 10-35 a second).
- The fragment cache kept its fragments in a vector sorted by address, so
  every new fragment moved half of it (thousands of entries); lookups that
  miss the hash table used a binary search over it.  It now appends, and
  looks addresses up in a hash map; the pending-link and hot-trace maps are
  hash maps too.  Inserting and linking took 9.2% of core 0 in the busiest
  compile window of a profiled GoldenEye run and now 0.9%; compile time per
  fragment there ~785 -> ~510 us.
- A new fragment and each link patch clean and invalidate only their own
  cache lines (through the Luma kernel backdoor) instead of the whole data and
  instruction caches.

GoldenEye Dam after these: 11.4-15.2 guest MIPS (11.6-12.9 before).

On an Old 3DS GoldenEye now reaches the Dam mission and runs it at about
12-19 presents/s (7-11 before the dynarec changes); the intro and menus run
at 15-30.

Saves no longer stop the game: writing a save file to the SD card takes up
to seconds on an Old 3DS (file creation, rename and data each go through the
FS service), and it ran on the CPU thread from the VI handler.  The contents
are now copied there and written by a separate thread; closing the game
waits for it.  Every EEPROM block and SRAM DMA also logged a line to the SD
card, which is no longer done on 3DS.

The costliest audio commands have ARM11 versions of their inner loops: the
envelope mixers (word-wide access to sample pairs, SSAT/QADD16 saturation,
SMULWB/SMULWT scaling, constant gains once a volume ramp has settled; the
original loop remains for unusual buffer layouts) and the ADPCM predictor
shared by the three ABIs (16-bit multiply-accumulates on packed halfwords,
SMLAxy/SMLADX, instead of 44 32-bit multiplies per 8 samples).
`tests/test_audio_hle.py` builds the upstream and the patched commands for
the host and checks that they produce the same bytes on random inputs.
Audio thread share of core 1 in comparable windows: GoldenEye (ABI1) Dam
34% -> 23% (core 0's wait for it 445 -> ~120 ms per 10 s), Mario Kart 64
(ABI2) 54% -> 43%, Harvest Moon 64 (ABI3) 41% -> 38%.

`BaseRenderer::FlushTris` no longer allocates and frees its vertex buffer
for every draw (thousands a second; the heap lock is shared with core 0):
about 5% of core 1 in Harvest Moon 64 and Super Mario 64.

With the CPU faster, core 1 (graphics and audio HLE, which may use at most
80% of it) is the limit in several games.  Profiled runs sample it too;
Dam in GoldenEye: audio 35%, graphics 35%, idle 31%; Harvest Moon 64 attract:
audio 40%, graphics 47%; Mario Kart 64 attract: audio 18%, graphics 47% (of
which texture uploads 11%).

### Console test tools

These files in `sdmc:/3ds/n64to3ds-agent/` are read when a game starts and
removed, so each one affects one run:

| File | Content | Effect |
| --- | --- | --- |
| `autoreturn` | seconds | quit after that long and return to the n64to3ds agent (`tools/console-agent`), which serves the logs |
| `profile` | anything | log an `n64to3ds prof:` line per diag window: share of core 0 per phase (dynarec, interpreter, TLB, waits...), share of core 1 per phase (`c1_audio`, `c1_dl`, `c1_vertex`, `c1_tris`, `c1_prepare`, `c1_draw`, `c1_combine`, `c1_picagl`, `c1_texture`, `c1_texupload`, ...; the audio and graphics threads drop one priority step so the sampler can preempt them), display list handler time by GBI command (`c1_cmds`), draws and vertices per window (`gfx_flushes`, `gfx_flush_verts`), most sampled guest PCs, interpreted PCs, instructions the dynarec hands to the interpreter, audio commands by core 1 time.  Costs about 20% of the emulation speed while active |
| `screenshots` | seconds | save the top screen every that many seconds as `shot_NN.bmp`; each one stops the game for about 2 s while it is written |
| `inputs` | lines `<start s> <duration s> KEY[+KEY]` or `<start s> <duration s> STICK dx dy` | press 3DS keys or hold the circle pad at those times, through the normal controller mapping |
| `codedump` | `<start s> <end s>` (with `profile`) | count fragment entries between the two times, then write the 800 most executed fragments (ARM code, MIPS opcodes, entry counts) to `codedump.bin`; `scripts/codedump_report.py` reads it |
| (none) | | every run logs an `n64to3ds stall:` line for each frame that took more than 150 ms (at most 10 per diag window): new dynarec code and cache maintenance, interpreted instructions, textures converted, waits for core 1 and the longest graphics task in that frame |
| `dynarec_off` | mask | switch off dynarec code generation changes for A/B runs: 1 delay-slot flags, 2 deferred high words, 4 likely-branch exits, 8 inline direct exits, 16 fast indirect exits |

## Reproducibility and licenses

- Daedalus is pinned to commit
  `580c0c409358db332149fc2903c610449434f8fc`.
- The minimal Daedalus changes are in
  `patches/daedalus-n64to3ds.patch` and are applied by
  `scripts/bootstrap.sh`.
- Python dependencies are pinned in `requirements.txt`.
- The manifest records the upstream commit, toolchain versions, ROM SHA-256,
  generated block counts, hypothesis metadata, verification results, and any
  runtime evidence that was recorded.
- DaedalusX64 is licensed under GPL-2.0.  This project is distributed under
  GPL-2.0-or-later.  See `LICENSE`, `NOTICE`, and `THIRD_PARTY_NOTICES.md`.
- Rabbitizer and ipl3checksum are MIT-licensed.
- devkitPro components have their own licenses; they are not vendored in this
  repository when installed system-wide.  The optional `--from-image` path
  extracts the pinned image locally and does not redistribute it.

Do **not** commit ROMs, BIOS/firmware, keys, or proprietary game assets.  The
repository `.gitignore` excludes common ROM and CIA extensions.

## Limitations

- **No physical hardware verification.**  Every build made in this environment
  must be reported as `hardware-unverified`.
- **No playability or full-speed claim.**  Older builds crashed under Azahar right
  after the first frame (Undefined Instruction in Dynarec code), because Azahar
  does not implement `svcCustomBackdoor` and the dynarec's instruction-cache
  flush was a no-op there.  The runtime now uses
  `svcInvalidateEntireInstructionCache` when it detects an emulator.  With that
  fix, async audio HLE on core 1, and a proven poll-loop skip in the JIT, Azahar
  in Old 3DS mode renders the HM64 intro at about 25-27 FPS, about 54 of 60
  VI/s.  Azahar's CPU timing is not hardware timing, and no input or gameplay
  session was tested.
- **`--aot off` boot is unverified.**  The structurally valid AOT-off HM64 build
  did not reach the end-of-frame callback within 10 minutes under Azahar, while
  the AOT-auto build did.  This is not proof that it fails on hardware, but it
  is not evidence that Dynarec-only boot works.
- **AOT does not speed up gameplay.**  Statically discovered blocks cover the
  boot code.  The game's main loop is reached through thread entry pointers and
  runs in Daedalus JIT fragments, so the AOT dispatcher almost only misses there.
- **Synthetic validation is structural.**  The synthetic second ROM proves the
  CLI, normalization, AOT analysis, packaging, and verification path are
  game-agnostic.  It is not a playable image and was not used for boot claims.
- **Static coverage is limited to the first 1 MiB.**  The analyzer maps the
  standard IPL3-loaded region.  Overlays and code DMA'd elsewhere stay on
  Daedalus Dynarec/interpreter.
- **Conservative fallback.**  CP0, FPU, cache/TLB, traps, unaligned memory
  operations, and branch-likely instructions are not AOT-compiled.
- **Mapping is heuristic.**  Multiple hypotheses are emitted and selected by
  runtime fingerprints; a mapping the analyzer never considered is not
  compiled.
- **Saves use Daedalus' existing save path.**  The save name is made unique via
  the RomFS filename, but save compatibility across emulator/hardware runs is
  not separately tested.
- **The local emulator evidence is Azahar, not hardware.**  Azahar is a testing
  aid and is not a project dependency.

## Tests

```bash
python3 -m pytest
```

The suite currently contains 47 tests.  They cover ROM normalization, the
multi-hypothesis analyzer, code generation, the host AOT semantic harness, title
IDs and collision handling, RomFS/IVFC wrapping and corruption detection, ROM
database lookup, runtime evidence parsing, and CLI behavior.
`tests/test_audio_hle.py` (needs `g++` and the bootstrapped Daedalus tree)
checks that the patched audio commands match upstream DaedalusX64 byte for
byte.

## Legal

This tool does not include or distribute any Nintendo ROM, BIOS, firmware,
encryption key, or proprietary asset.  You must supply your own legally
obtained ROM.  DaedalusX64 and its components remain under their respective
licenses.

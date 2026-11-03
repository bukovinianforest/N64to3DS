"""Audio HLE rewrites are bit-identical to upstream DaedalusX64.

The n64to3ds patch replaces the inner loops of the costliest audio commands
with faster ones for the ARM11.  These tests build the upstream version (the
pinned commit the patch applies to) and the patched version of each command
for the host and compare their output on random inputs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DAEDALUS = ROOT / "third_party" / "daedalus"
HARNESS = ROOT / "tests" / "audio_hle"
CXX = shutil.which("g++")

FLAGS = [
    "-std=c++20",
    "-O2",
    "-fwrapv",  # the commands rely on wrapping s32 arithmetic, as the ARM build does
    "-w",
    f"-I{DAEDALUS / 'Source'}",
    "-DDAEDALUS_ENDIAN_LITTLE=1",
    "-DDAEDALUS_LINUX",
]

HEADERS = """#include "Base/Types.h"
#include <string.h>
#include <cstring>
#include "Utility/MathUtil.h"
#include "Core/Memory.h"
#include "Debug/DBGConsole.h"
#include "HLEAudio/HLEAudioInternal.h"
#include "HLEAudio/HLEAudioState.h"
"""


def _upstream(path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(DAEDALUS), "show", f"HEAD:{path}"], capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else None


needs_tree = pytest.mark.skipif(
    CXX is None or not (DAEDALUS / ".git").exists() or not (DAEDALUS / "Source" / "HLEAudio" / "EnvMixerFast.h").is_file(),
    reason="needs g++ and the bootstrapped, patched Daedalus tree",
)


def _build(tmp_path: Path, name: str, sources: dict[str, str], main: Path) -> Path:
    objects = []
    for stem, text in sources.items():
        source = tmp_path / f"{stem}.cpp"
        source.write_text(text)
        objects.append(tmp_path / f"{stem}.o")
        subprocess.run([CXX, *FLAGS, f"-I{tmp_path}", "-c", str(source), "-o", str(objects[-1])], check=True)
    for extra in (main, HARNESS / "stubs.cpp"):
        objects.append(tmp_path / f"{extra.stem}.o")
        subprocess.run([CXX, *FLAGS, "-c", str(extra), "-o", str(objects[-1])], check=True)
    binary = tmp_path / name
    subprocess.run([CXX, *map(str, objects), "-o", str(binary)], check=True)
    return binary


def _run(binary: Path, iterations: int) -> str:
    result = subprocess.run([str(binary), str(iterations)], capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    assert " 0 mismatches" in result.stdout
    return result.stdout


@needs_tree
def test_envmixer_abi1_matches_upstream(tmp_path):
    upstream = _upstream("Source/HLEAudio/HLEAudioState.cpp")
    if upstream is None:
        pytest.skip("upstream HLEAudioState.cpp not available")
    (tmp_path / "ref_HLEAudioState.cpp").write_text(upstream)
    patched = DAEDALUS / "Source" / "HLEAudio" / "HLEAudioState.cpp"
    sources = {
        "ref_tu": "#define AudioHLEState RefAudioHLEState\n#define gAudioHLEState gRefAudioHLEState\n"
        '#include "ref_HLEAudioState.cpp"\n'
        "void RefEnvMixer(void * s, u8 flags, u32 address) { static_cast<RefAudioHLEState *>(s)->EnvMixer(flags, address); }\n",
        "new_tu": "#define AudioHLEState NewAudioHLEState\n#define gAudioHLEState gNewAudioHLEState\n"
        "#define SPNOOP NewSPNOOP\n#define UNKNOWN NewUNKNOWN\n"
        f'#include "{patched}"\n'
        "void NewEnvMixer(void * s, u8 flags, u32 address) { static_cast<NewAudioHLEState *>(s)->EnvMixer(flags, address); }\n",
    }
    binary = _build(tmp_path, "envmixer", sources, HARNESS / "envmixer_main.cpp")
    print(_run(binary, 20000))


@needs_tree
def test_envmixer2_abi2_matches_upstream(tmp_path):
    upstream = _upstream("Source/HLEAudio/ENV_Mixer.cpp")
    if upstream is None:
        pytest.skip("upstream ENV_Mixer.cpp not available")
    (tmp_path / "ref_ENV_Mixer.cpp").write_text(upstream)
    patched = DAEDALUS / "Source" / "HLEAudio" / "ENV_Mixer.cpp"
    # Both versions in their own namespace; their includes are already done.
    sources = {
        "ref_env": HEADERS + '#include "HLEAudio/EnvMixerFast.h"\nnamespace ref {\n#include "ref_ENV_Mixer.cpp"\nbool isMKABI = false;\n}\n',
        "new_env": HEADERS + f'#include "HLEAudio/EnvMixerFast.h"\nnamespace neu {{\n#include "{patched}"\nbool isMKABI = false;\n}}\n',
    }
    binary = _build(tmp_path, "envmixer2", sources, HARNESS / "envmixer2_main.cpp")
    print(_run(binary, 20000))


@needs_tree
def test_adpcm_all_abis_match_upstream(tmp_path):
    state = _upstream("Source/HLEAudio/HLEAudioState.cpp")
    adpcm = _upstream("Source/HLEAudio/ABI_ADPCM.cpp")
    if state is None or adpcm is None:
        pytest.skip("upstream audio sources not available")
    (tmp_path / "ref_HLEAudioState.cpp").write_text(state)
    (tmp_path / "ref_ABI_ADPCM.cpp").write_text(adpcm)
    source = DAEDALUS / "Source" / "HLEAudio"
    sources = {
        "ref_tu": "#define AudioHLEState RefAudioHLEState\n#define gAudioHLEState gRefAudioHLEState\n"
        '#include "ref_HLEAudioState.cpp"\n'
        "void RefADPCMDecode(void * s, u8 flags, u32 address) { static_cast<RefAudioHLEState *>(s)->ADPCMDecode(flags, address); }\n",
        "new_tu": "#define AudioHLEState NewAudioHLEState\n#define gAudioHLEState gNewAudioHLEState\n"
        "#define SPNOOP NewSPNOOP\n#define UNKNOWN NewUNKNOWN\n"
        f'#include "{source / "HLEAudioState.cpp"}"\n'
        "void NewADPCMDecode(void * s, u8 flags, u32 address) { static_cast<NewAudioHLEState *>(s)->ADPCMDecode(flags, address); }\n",
        "ref_adpcm": HEADERS + '#include "HLEAudio/AdpcmFast.h"\nnamespace ref {\n#include "ref_ABI_ADPCM.cpp"\n'
        "bool isMKABI = false;\nbool isZeldaABI = false;\n}\n",
        "new_adpcm": HEADERS + f'#include "HLEAudio/AdpcmFast.h"\nnamespace neu {{\n#include "{source / "ABI_ADPCM.cpp"}"\n'
        "bool isMKABI = false;\nbool isZeldaABI = false;\n}\n",
    }
    binary = _build(tmp_path, "adpcm", sources, HARNESS / "adpcm_main.cpp")
    print(_run(binary, 20000))

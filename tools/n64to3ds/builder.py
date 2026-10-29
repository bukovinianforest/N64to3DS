"""End-to-end ROM -> CIA build orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .analyzer import AnalysisResult, analyze, analyze_multi
from .codegen import GeneratedProgram, ProgramMetadata, generate_program, write_generated, write_manifest
from .errors import BuildError, N64To3DSError, ToolchainError, VerificationError
from .extensions import select_extension, write_romfs_profile
from .kit import load_recipe, run_recipe
from .manifest import BuildManifest, Status, host_info
from .rom import NormalizedRom, load_rom, write_normalized
from .romdb import (
    RomDbResult,
    generate_roms_ini,
    upstream_roms_ini_path,
)
from .romfs import build_romfs
from .titleid import choose_title_id, format_title_id, format_unique_id
from .verify import CiaInfo, verify_cia

REPO_ROOT = Path(__file__).resolve().parents[2]
DAEDALUS_ROOT = REPO_ROOT / "third_party" / "daedalus"
DAEDALUS_SOURCE = DAEDALUS_ROOT
RUNTIME_DIR = REPO_ROOT / "runtime" / "3ds"
HOST_TEST_SOURCE = REPO_ROOT / "tests" / "host" / "aot_host.cpp"
BUILD_ROOT = REPO_ROOT / "build"
# Present only in a release bundle: the recorded Daedalus build (see kit.py).
KIT_RECIPE = REPO_ROOT / "kit" / "recipe.json"

DEFAULT_ANALYSIS_BLOCKS = 4096
DEFAULT_ANALYSIS_INSTRUCTIONS = 16384
DEFAULT_HOST_TEST_ITERATIONS = 100

SAVE_TYPES = {
    "eeprom4k": "Eeprom4k",
    "eeprom16k": "Eeprom16k",
    "sram": "SRAM",
    "flashram": "FlashRam",
    "flash": "FlashRam",
    "unknown": "Unknown",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_app_name(rom: NormalizedRom) -> str:
    name = rom.header.name or rom.source_path.stem
    name = name.replace("\x00", "").strip()
    if not name:
        name = "N64 ROM"
    return name[:64]


def _ascii_title(name: str) -> str:
    value = name.encode("ascii", errors="replace").decode("ascii")
    value = re.sub(r"[\r\n\t]+", " ", value).strip()
    return value[:64] or "N64to3DS"


def _sanitize_product_code(unique: int) -> str:
    return f"CTR-P-{unique & 0xFFFF:04X}"


@dataclass
class Toolchain:
    devkitpro: Path
    devkitarm: Path
    env: dict[str, str]
    cmake: str | None
    gxx: str | None
    mkromfs3ds: str
    smdhtool: str
    makerom: str
    three_dsxtool: str
    versions: dict[str, str] = field(default_factory=dict)
    # Release bundle: Daedalus is built from this recipe instead of CMake.
    kit_recipe: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "DEVKITPRO": str(self.devkitpro),
            "DEVKITARM": str(self.devkitarm),
            "cmake": self.cmake,
            "kit_recipe": str(self.kit_recipe) if self.kit_recipe else None,
            "host_gxx": self.gxx,
            "mkromfs3ds": self.mkromfs3ds,
            "smdhtool": self.smdhtool,
            "makerom": self.makerom,
            "3dsxtool": self.three_dsxtool,
            "versions": dict(self.versions),
        }


def _which(name: str, env: dict[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH"))


def _discover_bundle_toolchain() -> Toolchain:
    """Use only the tools shipped in a release bundle, never a host devkitPro.

    The recipe was recorded against exactly these devkitARM and library
    versions, so DEVKITPRO/DEVKITARM from the environment are ignored.
    """

    devkitpro = (REPO_ROOT / "third_party" / "devkitpro").resolve()
    devkitarm = devkitpro / "devkitARM"
    bundled_path = os.pathsep.join([str(devkitarm / "bin"), str(devkitpro / "tools" / "bin")])
    found: dict[str, str] = {}
    for name in ("arm-none-eabi-g++", "mkromfs3ds", "smdhtool", "3dsxtool", "makerom"):
        tool = shutil.which(name, path=bundled_path)
        if not tool:
            raise ToolchainError(
                f"The release bundle is incomplete: {name} was not found in {bundled_path}.",
                hint="Download the release archive again and extract all of it.",
            )
        found[name] = tool
    if not (devkitpro / "picaGL" / "lib").is_dir():
        raise ToolchainError(
            f"The release bundle is incomplete: {devkitpro / 'picaGL'} is missing.",
            hint="Download the release archive again and extract all of it.",
        )
    if os.name == "nt":
        non_ascii = [str(path) for path in (REPO_ROOT, BUILD_ROOT) if not str(path).isascii()]
        if non_ascii:
            raise ToolchainError(
                f"The bundle path contains non-ASCII characters: {non_ascii[0]}",
                hint=(
                    "devkitARM on Windows cannot open such paths reliably.  Extract "
                    "the release to a plain path such as C:\\n64to3ds and run it from there."
                ),
            )

    env = dict(os.environ)
    env["DEVKITPRO"] = str(devkitpro)
    env["DEVKITARM"] = str(devkitarm)
    env["PATH"] = os.pathsep.join([bundled_path, env.get("PATH", "")])
    if os.name == "nt":
        # GCC's LTO temporaries default to %TEMP%, which is under the user
        # profile and may be non-ASCII; keep them in the checked bundle path.
        temp = BUILD_ROOT / "tmp"
        temp.mkdir(parents=True, exist_ok=True)
        env["TMP"] = env["TEMP"] = env["TMPDIR"] = str(temp)
    # The AOT host self-test needs a native g++; tests/host/aot_host.cpp is
    # only exercised with GCC on Linux, so it is not attempted on Windows.
    gxx = None if os.name == "nt" else shutil.which("g++")
    toolchain = Toolchain(
        devkitpro=devkitpro,
        devkitarm=devkitarm,
        env=env,
        cmake=None,
        gxx=gxx,
        mkromfs3ds=found["mkromfs3ds"],
        smdhtool=found["smdhtool"],
        makerom=found["makerom"],
        three_dsxtool=found["3dsxtool"],
        kit_recipe=KIT_RECIPE,
    )
    toolchain.versions = _toolchain_versions(toolchain)
    return toolchain


def discover_toolchain(*, require_host_compiler: bool = False) -> Toolchain:
    if KIT_RECIPE.is_file():
        return _discover_bundle_toolchain()
    devkitpro_value = os.environ.get("DEVKITPRO")
    if devkitpro_value:
        devkitpro = Path(devkitpro_value).expanduser()
    elif (REPO_ROOT / "third_party" / "devkitpro").exists():
        devkitpro = REPO_ROOT / "third_party" / "devkitpro"
    elif Path("/opt/devkitpro").exists():
        devkitpro = Path("/opt/devkitpro")
    else:
        raise ToolchainError(
            "devkitPro was not found.",
            hint=(
                "Set DEVKITPRO (for example /opt/devkitpro) or run "
                "scripts/bootstrap.sh.  See README.md for the clean-Ubuntu setup."
            ),
        )

    devkitpro = devkitpro.resolve()
    devkitarm = Path(os.environ.get("DEVKITARM", devkitpro / "devkitARM")).expanduser()
    if not (devkitarm / "bin" / "arm-none-eabi-g++").is_file():
        raise ToolchainError(
            f"devkitARM was not found under {devkitarm}.",
            hint="Install devkitARM with devkitPro's pacman, or set DEVKITARM.",
        )
    if not (devkitpro / "picaGL").is_dir():
        raise ToolchainError(
            f"picaGL was not found under {devkitpro / 'picaGL'}.",
            hint=(
                "Daedalus' 3DS backend links picaGL.  Install the devkitPro "
                "package (or use scripts/bootstrap.sh with the pinned container image)."
            ),
        )

    env = dict(os.environ)
    path_parts = [
        str(devkitarm / "bin"),
        str(devkitpro / "tools" / "bin"),
    ]
    makerom_candidates = [
        devkitpro / "tools" / "bin" / "makerom",
        devkitpro.parents[1] / "usr" / "local" / "bin" / "makerom",
        Path("/usr/local/bin/makerom"),
        Path("/usr/bin/makerom"),
    ]
    makerom = None
    for candidate in makerom_candidates:
        if candidate.is_file():
            makerom = str(candidate)
            path_parts.insert(0, str(candidate.parent))
            break
    if makerom is None:
        found = shutil.which("makerom", path=env.get("PATH"))
        if found:
            makerom = found
            path_parts.insert(0, str(Path(found).parent))
    env["DEVKITPRO"] = str(devkitpro)
    env["DEVKITARM"] = str(devkitarm)
    env["PATH"] = os.pathsep.join(path_parts + [env.get("PATH", "")])

    cmake = _which("cmake", env)
    if not cmake:
        raise ToolchainError(
            "cmake was not found on PATH.",
            hint="Install cmake (apt install cmake) and retry.",
        )
    mkromfs3ds = _which("mkromfs3ds", env)
    if not mkromfs3ds:
        raise ToolchainError(
            "mkromfs3ds was not found.",
            hint="Install devkitPro tools (3ds-dev) or set DEVKITPRO correctly.",
        )
    smdhtool = _which("smdhtool", env)
    if not smdhtool:
        raise ToolchainError(
            "smdhtool was not found.",
            hint="Install devkitPro tools (3ds-dev) or set DEVKITPRO correctly.",
        )
    three_dsxtool = _which("3dsxtool", env)
    if not three_dsxtool:
        raise ToolchainError(
            "3dsxtool was not found.",
            hint="Install devkitPro tools (3ds-dev) or set DEVKITPRO correctly.",
        )
    if not makerom:
        raise ToolchainError(
            "makerom was not found.",
            hint=(
                "Install makerom (devkitPro's 3ds-dev package or the pinned "
                "Daedalus container image).  scripts/bootstrap.sh handles this."
            ),
        )
    gxx = _which("g++", env)
    if require_host_compiler and not gxx:
        raise ToolchainError(
            "A host g++ is required for the AOT semantic self-test.",
            hint="Install build-essential, or build with --aot off.",
        )

    toolchain = Toolchain(
        devkitpro=devkitpro,
        devkitarm=devkitarm,
        env=env,
        cmake=cmake,
        gxx=gxx,
        mkromfs3ds=mkromfs3ds,
        smdhtool=smdhtool,
        makerom=makerom,
        three_dsxtool=three_dsxtool,
    )
    toolchain.versions = _toolchain_versions(toolchain)
    return toolchain


def _run_capture(command: list[str], env: dict[str, str]) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, env=env, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout + result.stderr).strip()


def _toolchain_versions(toolchain: Toolchain) -> dict[str, str]:
    versions: dict[str, str] = {}
    gcc = _run_capture(
        [str(toolchain.devkitarm / "bin" / "arm-none-eabi-g++"), "--version"],
        toolchain.env,
    )
    if gcc:
        versions["devkitARM gcc"] = gcc.splitlines()[0]
    makerom = _run_capture([toolchain.makerom], toolchain.env)
    if makerom:
        versions["makerom"] = makerom.splitlines()[0]
    if toolchain.cmake:
        cmake = _run_capture([toolchain.cmake, "--version"], toolchain.env)
        if cmake:
            versions["cmake"] = cmake.splitlines()[0]
    if toolchain.gxx:
        host = _run_capture([toolchain.gxx, "--version"], toolchain.env)
        if host:
            versions["host g++"] = host.splitlines()[0]
    return versions


def _daedalus_commit() -> str | None:
    git_dir = DAEDALUS_ROOT / ".git"
    if not git_dir.exists():
        if KIT_RECIPE.is_file():
            return load_recipe(KIT_RECIPE).get("daedalus_commit")
        return None
    result = _run_capture(
        ["git", "-C", str(DAEDALUS_ROOT), "rev-parse", "HEAD"], dict(os.environ)
    )
    return result or None


def _run_logged(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_handle,
    verbose: bool,
    label: str,
) -> None:
    log_handle.write(f"\n$ {' '.join(command)}\n")
    log_handle.flush()
    if verbose:
        print(f"$ {' '.join(command)}")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise BuildError(f"Could not start {label}: {exc}") from exc
    assert process.stdout is not None
    tail: list[str] = []
    for line in process.stdout:
        log_handle.write(line)
        log_handle.flush()
        tail.append(line.rstrip())
        if len(tail) > 40:
            tail.pop(0)
        if verbose:
            print(line, end="")
    returncode = process.wait()
    if returncode != 0:
        detail = "\n".join(tail[-20:])
        raise BuildError(
            f"{label} failed with exit code {returncode}.",
            hint=(
                f"Last output:\n{detail}\n\n"
                "The full build log is kept with --keep-build; see the manifest log_path."
            ),
        )


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _copy_atomic(source: Path, destination: Path) -> None:
    _write_atomic(destination, source.read_bytes())


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _patch_rsf(
    template: str,
    *,
    title: str,
    unique_id: int,
    product_code: str,
) -> str:
    text = template
    substitutions = {
        r"^(\s*Title\s*:\s*).*$": lambda match: match.group(1) + _ascii_title(title),
        r"^(\s*ProductCode\s*:\s*).*$": lambda match: match.group(1) + product_code,
        r"^(\s*UniqueId\s*:\s*).*$": lambda match: match.group(1) + f"0x{unique_id:06X}",
        r"^(\s*EnableCompress\s*:\s*).*$": lambda match: match.group(1) + "false",
        # Cores 0 and 1 both exist on Old 3DS.  The audio HLE thread runs on
        # core 1, which the template's mask (core 0 only) would reject.
        r"^(\s*AffinityMask\s*:\s*).*$": lambda match: match.group(1) + "3",
        # The low 7 bits of MaxCpu cap APT_SetAppCpuTimeLimit (PM rejects any
        # higher value with 0xD8E05BF4); bit 7 keeps the multi-mode core 1
        # scheduling of the template's 0x9E.  0x9E allowed only 30%, which made
        # the core 1 audio thread slower than synchronous audio on Old 3DS.
        r"^(\s*MaxCpu\s*:\s*).*$": lambda match: match.group(1) + "0xD0 # multi-mode, core 1 limit up to 80%",
    }
    for pattern, replacement in substitutions.items():
        text, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
        if count == 0:
            raise BuildError(
                "Daedalus RSF template is missing an expected field.",
                hint=f"Pattern not found: {pattern}",
            )

    # Remove the New 3DS exclusive block.  It is not needed on Old 3DS and
    # requesting 804 MHz / L2 / core 2 would make the image non-representative
    # of the target hardware.
    new3ds_pattern = re.compile(
        r"^[ \t]*# New3DS Exclusive Process Settings.*?^[ \t]*CanAccessCore2[^\n]*\n",
        flags=re.MULTILINE | re.DOTALL,
    )
    text, count = new3ds_pattern.subn(
        "  # New3DS-only settings intentionally omitted (Old 3DS target).\n", text
    )
    if count == 0:
        raise BuildError(
            "Daedalus RSF template did not contain the expected New 3DS settings block."
        )
    for forbidden in ("SystemModeExt", "CpuSpeed", "EnableL2Cache", "CanAccessCore2"):
        if re.search(rf"^\s*{forbidden}\s*:", text, flags=re.MULTILINE):
            raise BuildError(
                f"Generated RSF still requests the New 3DS setting {forbidden}.",
                hint="The RSF template changed; update _patch_rsf() in builder.py.",
            )
    return text


def _create_smdh(
    *,
    toolchain: Toolchain,
    game_name: str,
    icon: Path,
    output: Path,
    log_handle,
    verbose: bool,
) -> None:
    long_description = f"N64 emulation via DaedalusX64: {game_name}"
    _run_logged(
        [
            toolchain.smdhtool,
            "--create",
            game_name[:64],
            long_description[:128],
            "n64to3ds / DaedalusX64 Team",
            str(icon),
            str(output),
        ],
        cwd=output.parent,
        env=toolchain.env,
        log_handle=log_handle,
        verbose=verbose,
        label="smdhtool",
    )


def _run_host_aot_test(
    *,
    toolchain: Toolchain,
    program: GeneratedProgram,
    rom: NormalizedRom,
    romfs_rom_path: Path,
    work_dir: Path,
    aot_source: Path,
    log_handle,
    verbose: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ran": False,
        "passed": False,
        "blocks": program.block_count,
        "executions": 0,
    }
    if program.block_count == 0:
        result["reason"] = "no generated blocks"
        return result
    if toolchain.gxx is None:
        result["reason"] = "host g++ not available"
        return result

    executable = work_dir / "aot_host_test"
    compile_command = [
        toolchain.gxx,
        "-std=c++17",
        "-O2",
        "-Wall",
        "-Wextra",
        "-DN64TO3DS_HOST_TEST",
        f"-I{RUNTIME_DIR / 'AOT'}",
        str(HOST_TEST_SOURCE),
        str(aot_source),
        "-o",
        str(executable),
    ]
    _run_logged(
        compile_command,
        cwd=work_dir,
        env=toolchain.env,
        log_handle=log_handle,
        verbose=verbose,
        label="AOT host semantic test compilation",
    )
    run_command = [
        str(executable),
        "--rom",
        str(romfs_rom_path),
        "--iterations",
        str(DEFAULT_HOST_TEST_ITERATIONS),
    ]
    # The host test needs the normalized ROM.  Use the copy in RomFS so the
    # exact embedded bytes are tested, not the user's source file.
    _run_logged(
        run_command,
        cwd=work_dir,
        env=toolchain.env,
        log_handle=log_handle,
        verbose=verbose,
        label="AOT host semantic test",
    )
    result["ran"] = True
    result["passed"] = True
    result["executions"] = program.block_count * DEFAULT_HOST_TEST_ITERATIONS
    return result


@dataclass
class BuildOptions:
    rom_path: Path
    output_path: Path
    target: str = "old3ds"
    aot_mode: str = "auto"
    verbose: bool = False
    keep_build: bool = False
    build_dir: Path | None = None
    title_id: str | None = None
    # HOME menu name (SMDH short title); the ROM header name by default.
    app_name: str | None = None
    jobs: int = max(1, min(os.cpu_count() or 1, 16))
    rom_db: Path | None = None
    save_type: str | None = None
    use_registry: bool = True
    # Dual-screen extensions: "auto" (use a verified profile if one matches
    # the ROM), "off", or an explicit extension id.
    extensions_mode: str = "auto"


def cmake_configure_command(
    *,
    cmake: str,
    devkitpro: Path,
    build_dir: Path,
    romfs_name: str,
    title_id_text: str,
    smdh: Path,
    banner: Path,
    rsf: Path,
    romfs_dir: Path,
    dualscreen: bool,
    ivfc_romfs: Path,
    output_3dsx: Path,
    output_cia: Path,
    aot_source: Path | None,
) -> list[str]:
    """The Daedalus configure command; the release recipe is recorded from it."""

    return [
        cmake,
        "-S",
        str(DAEDALUS_SOURCE),
        "-B",
        str(build_dir),
        f"-DCMAKE_TOOLCHAIN_FILE={devkitpro / 'cmake' / '3DS.cmake'}",
        "-DCTR=1",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DN64TO3DS_RUNTIME_DIR={RUNTIME_DIR}",
        f"-DN64TO3DS_AUTOBOOT_ROM={romfs_name}",
        f"-DN64TO3DS_TITLE_ID={title_id_text}",
        f"-DN64TO3DS_SMDH={smdh}",
        f"-DN64TO3DS_BANNER={banner}",
        f"-DN64TO3DS_RSF={rsf}",
        f"-DN64TO3DS_ROMFS_DIR={romfs_dir}",
        f"-DN64TO3DS_DUALSCREEN={'ON' if dualscreen else 'OFF'}",
        f"-DN64TO3DS_ROMFS_BIN={ivfc_romfs}",
        f"-DN64TO3DS_OUTPUT_3DSX={output_3dsx}",
        f"-DN64TO3DS_OUTPUT_CIA={output_cia}",
        f"-DN64TO3DS_AOT_SOURCE={aot_source if aot_source is not None else ''}",
    ]


def _build_from_recipe(
    *,
    toolchain: Toolchain,
    options: BuildOptions,
    work: Path,
    build_dir: Path,
    romfs_name: str,
    title_id_text: str,
    aot_source: Path | None,
    dualscreen: bool,
    smdh: Path,
    banner: Path,
    rsf: Path,
    romfs_dir: Path,
    ivfc_romfs: Path,
    log_handle,
) -> None:
    """Release-bundle replacement for the CMake build and its post-build steps."""

    assert toolchain.kit_recipe is not None
    recipe = load_recipe(toolchain.kit_recipe)

    def log(text: str) -> None:
        log_handle.write(text)
        log_handle.flush()

    def run(command: list[str], cwd: Path, label: str) -> None:
        _run_logged(
            command,
            cwd=cwd,
            env=toolchain.env,
            log_handle=log_handle,
            verbose=options.verbose,
            label=label,
        )

    run_recipe(
        recipe,
        aot=aot_source is not None,
        dualscreen=dualscreen,
        values={
            "DEVKITPRO": toolchain.devkitpro.as_posix(),
            "DAEDALUS": DAEDALUS_SOURCE.as_posix(),
            "RUNTIME": RUNTIME_DIR.as_posix(),
            "BUILD": build_dir.as_posix(),
            "AOT_SOURCE": aot_source.as_posix() if aot_source is not None else "",
            "AUTOBOOT_ROM": romfs_name,
            "TITLE_ID": title_id_text,
        },
        env=toolchain.env,
        jobs=options.jobs,
        log=log,
        verbose=options.verbose,
        run_link=lambda command, cwd: run(command, cwd, "Daedalus link"),
    )
    # The same commands as the CMake POST_BUILD steps in Source/CMakeLists.txt.
    source_build = build_dir / "Source"
    run(
        [
            toolchain.three_dsxtool,
            "daedalus.elf",
            str(work / "DaedalusX64.3dsx"),
            f"--romfs={romfs_dir}",
            f"--smdh={smdh}",
        ],
        source_build,
        "3dsxtool",
    )
    run(
        [
            toolchain.makerom,
            "-f",
            "cia",
            "-target",
            "t",
            "-exefslogo",
            "-o",
            str(work / "DaedalusX64.cia"),
            "-elf",
            "daedalus.elf",
            "-rsf",
            str(rsf),
            "-banner",
            str(banner),
            "-icon",
            str(smdh),
            "-romfs",
            str(ivfc_romfs),
        ],
        source_build,
        "makerom",
    )


def _prepare_work_dir(options: BuildOptions) -> tuple[Path, bool]:
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    if options.build_dir is not None:
        work = options.build_dir.expanduser().resolve()
        work.mkdir(parents=True, exist_ok=True)
        return work, True
    work = Path(tempfile.mkdtemp(prefix="n64to3ds-", dir=str(BUILD_ROOT)))
    return work, options.keep_build


def _copy_log_next_to_output(log_path: Path, output: Path) -> Path:
    destination = output.with_name(output.name + ".build.log")
    _copy_file(log_path, destination)
    return destination


def _artifact_info(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def build(options: BuildOptions) -> BuildManifest:
    if options.target != "old3ds":
        raise BuildError(
            f"Unsupported target {options.target!r}.",
            hint="This tool targets Old 3DS/2DS only (ARMv6K).",
        )
    if options.aot_mode not in ("off", "auto"):
        raise BuildError("AOT mode must be 'off' or 'auto'.")

    save_type = None
    if options.save_type and options.save_type.lower() != "auto":
        save_type = SAVE_TYPES.get(options.save_type.lower())
        if save_type is None:
            raise BuildError(
                f"Unknown save type {options.save_type!r}.",
                hint="Use auto, Eeprom4k, Eeprom16k, SRAM, FlashRam, or Unknown.",
            )

    rom = load_rom(options.rom_path)
    game_name = _ascii_title(options.app_name) if options.app_name else _safe_app_name(rom)
    full_title_id, unique_id, title_warnings = choose_title_id(
        rom.sha256,
        game_name,
        options.title_id,
        use_registry=options.use_registry,
    )
    title_id_text = format_title_id(full_title_id)
    output = options.output_path.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    work, preserve_work = _prepare_work_dir(options)
    log_path = work / "build.log"
    log_handle = log_path.open("w", encoding="utf-8")
    warnings = list(rom.warnings) + list(title_warnings)
    manifest = BuildManifest(
        host=host_info(),
        rom=rom.as_dict(),
        title={
            "name": game_name,
            "title_id": title_id_text,
            "unique_id": format_unique_id(unique_id),
            "product_code": _sanitize_product_code(unique_id),
            "romfs_path": f"{title_id_text}.z64",
        },
        upstream={
            "daedalus_path": str(DAEDALUS_ROOT),
            "daedalus_commit": _daedalus_commit(),
        },
    )
    manifest.warnings = warnings
    log_handle.write(f"n64to3ds build\n")
    log_handle.write(f"rom={rom.source_path}\n")
    log_handle.write(f"sha256={rom.sha256}\n")
    log_handle.write(f"title_id={title_id_text}\n")
    log_handle.write(f"work_dir={work}\n")
    log_handle.flush()

    try:
        require_host = options.aot_mode == "auto"
        toolchain = discover_toolchain(require_host_compiler=require_host)
        manifest.toolchain = toolchain.as_dict()

        resources = DAEDALUS_ROOT / "Source" / "SysCTR" / "Resources"
        banner = resources / "banner.bnr"
        icon = resources / "icon.png"
        font = resources / "romfs" / "Roboto-Medium.ttf"
        rsf_template = resources / "template.rsf"
        for required in (banner, icon, font, rsf_template):
            if not required.is_file():
                raise BuildError(
                    f"Required Daedalus resource is missing: {required}",
                    hint="The pinned Daedalus tree is incomplete; re-run scripts/bootstrap.sh.",
                )

        romfs_dir = work / "romfs"
        if romfs_dir.exists():
            shutil.rmtree(romfs_dir)
        romfs_dir.mkdir(parents=True)
        romfs_name = f"{title_id_text}.z64"
        romfs_rom_path = romfs_dir / romfs_name
        write_normalized(rom, romfs_rom_path)
        _copy_file(font, romfs_dir / "Roboto-Medium.ttf")

        extension = select_extension(options.extensions_mode, rom)
        if extension is not None:
            profile_file = write_romfs_profile(extension, romfs_dir, rom)
            manifest.extensions = {**extension.as_dict(), "romfs_profile": str(profile_file.relative_to(romfs_dir))}
            log_handle.write(f"dual-screen extension={extension.extension} profile={extension.profile_id}\n")
        else:
            manifest.extensions = {"extension": None, "mode": options.extensions_mode}

        upstream_db = options.rom_db.expanduser().resolve() if options.rom_db else upstream_roms_ini_path(REPO_ROOT)
        if options.rom_db and not upstream_db.is_file():
            raise BuildError(f"Custom ROM database not found: {upstream_db}")
        db_result = generate_roms_ini(
            rom,
            upstream_db,
            romfs_dir / "roms.ini",
            save_type_override=save_type,
            game_name=game_name,
        )
        warnings.extend(db_result.warnings)
        manifest.rom["rom_db"] = {
            "runtime_key": db_result.runtime_key,
            "header_key": db_result.header_key,
            "matched_key": db_result.matched_key,
            "save_type": db_result.save_type,
        }
        log_handle.write(
            f"romdb runtime_key={db_result.runtime_key} "
            f"header_key={db_result.header_key} "
            f"matched={db_result.matched_key} save_type={db_result.save_type}\n"
        )

        raw_romfs = work / "romfs.raw"
        ivfc_romfs = work / "romfs.bin"
        build_romfs(
            romfs_dir,
            raw_romfs,
            ivfc_romfs,
            mkromfs3ds=toolchain.mkromfs3ds,
            runner=lambda command: _run_logged(
                command,
                cwd=work,
                env=toolchain.env,
                log_handle=log_handle,
                verbose=options.verbose,
                label="mkromfs3ds",
            ),
        )

        smdh = work / "app.smdh"
        _create_smdh(
            toolchain=toolchain,
            game_name=game_name,
            icon=icon,
            output=smdh,
            log_handle=log_handle,
            verbose=options.verbose,
        )

        rsf = work / "app.rsf"
        rsf.write_text(
            _patch_rsf(
                rsf_template.read_text(encoding="utf-8"),
                title=game_name,
                unique_id=unique_id,
                product_code=_sanitize_product_code(unique_id),
            ),
            encoding="utf-8",
        )

        aot_source: Path | None = None
        aot_program: GeneratedProgram | None = None
        aot_info: dict[str, Any] = {
            "mode": options.aot_mode,
            "blocks": 0,
            "instructions": 0,
            "host_semantic_test": {"ran": False, "passed": False},
        }
        if options.aot_mode == "auto":
            analysis: AnalysisResult = analyze_multi(
                rom,
                max_hypotheses=2,
                max_blocks=DEFAULT_ANALYSIS_BLOCKS,
                max_instructions=DEFAULT_ANALYSIS_INSTRUCTIONS,
            )
            warnings.extend(analysis.warnings)
            metadata = ProgramMetadata(
                rom_sha256=rom.sha256,
                mapping_base=analysis.mapping.base_vaddr,
                mapped_size=analysis.mapping.size,
                game_name=game_name,
                aot_mode="auto",
                hypotheses=tuple(analysis.hypotheses),
            )
            aot_program = generate_program(analysis.selected, metadata)
            aot_dir = work / "aot"
            aot_source = aot_dir / "AOTGenerated.cpp"
            write_generated(aot_program, aot_source)
            write_manifest(aot_program, aot_dir / "aot.json")
            (work / "analysis.json").write_text(
                json.dumps(analysis.as_dict(), indent=2) + "\n", encoding="utf-8"
            )
            aot_info = {
                "mode": "auto",
                "blocks": aot_program.block_count,
                "instructions": aot_program.instruction_count,
                "primary_mapping": f"0x{analysis.mapping.base_vaddr:08X}",
                "mapped_size": analysis.mapping.size,
                "hypotheses": analysis.hypotheses,
                "stats": analysis.stats,
                "host_semantic_test": {"ran": False, "passed": False},
            }
            log_handle.write(
                f"aot: blocks={aot_program.block_count} "
                f"instructions={aot_program.instruction_count} "
                f"primary_mapping=0x{analysis.mapping.base_vaddr:08X} "
                f"size={analysis.mapping.size} "
                f"hypotheses={len(analysis.hypotheses)}\n"
            )
            for hypothesis in analysis.hypotheses:
                log_handle.write(
                    f"aot hypothesis: base={hypothesis['base_vaddr']} "
                    f"score={hypothesis['score']} "
                    f"selected_blocks={hypothesis['selected_blocks']} "
                    f"selected_instructions={hypothesis['selected_instructions']}\n"
                )
            host_result = _run_host_aot_test(
                toolchain=toolchain,
                program=aot_program,
                rom=rom,
                romfs_rom_path=romfs_rom_path,
                work_dir=work,
                aot_source=aot_source,
                log_handle=log_handle,
                verbose=options.verbose,
            )
            # The generated RomFS path is title-id based; the test uses it.
            aot_info["host_semantic_test"] = host_result
            if not host_result["ran"] and toolchain.gxx is None:
                warnings.append(
                    "AOT host semantic self-test skipped: no host g++ "
                    "(the generated blocks were not cross-checked on this machine)."
                )
        manifest.aot = aot_info

        build_dir = work / "daedalus-build"
        if toolchain.kit_recipe is not None:
            _build_from_recipe(
                toolchain=toolchain,
                options=options,
                work=work,
                build_dir=build_dir,
                romfs_name=romfs_name,
                title_id_text=title_id_text,
                aot_source=aot_source,
                dualscreen=extension is not None,
                smdh=smdh,
                banner=banner,
                rsf=rsf,
                romfs_dir=romfs_dir,
                ivfc_romfs=ivfc_romfs,
                log_handle=log_handle,
            )
        else:
            _run_logged(
                cmake_configure_command(
                    cmake=toolchain.cmake or "cmake",
                    devkitpro=toolchain.devkitpro,
                    build_dir=build_dir,
                    romfs_name=romfs_name,
                    title_id_text=title_id_text,
                    smdh=smdh,
                    banner=banner,
                    rsf=rsf,
                    romfs_dir=romfs_dir,
                    dualscreen=extension is not None,
                    ivfc_romfs=ivfc_romfs,
                    output_3dsx=work / "DaedalusX64.3dsx",
                    output_cia=work / "DaedalusX64.cia",
                    aot_source=aot_source,
                ),
                cwd=work,
                env=toolchain.env,
                log_handle=log_handle,
                verbose=options.verbose,
                label="CMake configure",
            )
            _run_logged(
                [
                    toolchain.cmake or "cmake",
                    "--build",
                    str(build_dir),
                    "--target",
                    "daedalus",
                    "--parallel",
                    str(options.jobs),
                ],
                cwd=work,
                env=toolchain.env,
                log_handle=log_handle,
                verbose=options.verbose,
                label="Daedalus 3DS build",
            )

        built_cia = work / "DaedalusX64.cia"
        built_3dsx = work / "DaedalusX64.3dsx"
        if not built_cia.is_file():
            raise BuildError(
                f"Build completed but {built_cia} was not created.",
                hint="Check the makerom output in the build log.",
            )
        verification = verify_cia(
            built_cia,
            rom,
            full_title_id,
            expected_rom_name=romfs_name,
        )
        if not verification.ok:
            raise VerificationError(
                "The generated CIA failed verification:\n- " + "\n- ".join(verification.errors),
                hint="The build directory was preserved for inspection.",
            )

        _copy_atomic(built_cia, output)
        if built_3dsx.is_file():
            _copy_atomic(built_3dsx, output.with_suffix(".3dsx"))
        if aot_source is not None:
            _copy_atomic(aot_source, output.with_name(output.name + ".aot.cpp"))
            _copy_atomic(work / "aot" / "aot.json", output.with_name(output.name + ".aot.json"))

        manifest_path = output.with_name(output.name + ".manifest.json")
        log_copy = output.with_name(output.name + ".build.log")
        manifest.log_path = str(log_copy)
        manifest.artifacts = {
            "cia": _artifact_info(output),
            "3dsx": _artifact_info(output.with_suffix(".3dsx")),
            "manifest": {"path": str(manifest_path)},
            "aot_source": _artifact_info(output.with_name(output.name + ".aot.cpp")),
            "aot_manifest": _artifact_info(output.with_name(output.name + ".aot.json")),
        }
        manifest.verification = verification.as_dict()
        manifest.runtime_evidence = {}
        manifest.status = Status(
            cia_built=True,
            emulator_boot_verified=False,
            aot_exercised=False,
            game_playable=False,
            full_speed=False,
            hardware_verified=False,
        )
        manifest.warnings = warnings
        manifest.write(manifest_path)

        log_handle.write(f"\nCIA built: {output}\n")
        log_handle.write(f"manifest: {manifest_path}\n")
        log_handle.write(f"log: {log_copy}\n")
        log_handle.write(
            "status: CIA built=yes emulator boot verified=no AOT exercised=no "
            "game playable=no full speed=no hardware-unverified\n"
        )
        log_handle.flush()
        _copy_log_next_to_output(log_path, output)
        return manifest
    except Exception as exc:
        try:
            log_handle.write(f"\nBUILD FAILED: {exc}\n")
            if isinstance(exc, N64To3DSError) and exc.hint:
                log_handle.write(f"HINT: {exc.hint}\n")
            log_handle.flush()
        except Exception:
            pass
        # Preserve the work directory on failure, even when the caller did not
        # ask for --keep-build, so the full log is available.
        print(f"Build failed; work directory preserved: {work}")
        raise
    finally:
        log_handle.close()
        if not preserve_work:
            shutil.rmtree(work, ignore_errors=True)


def analyze_rom(path: Path) -> tuple[NormalizedRom, AnalysisResult]:
    rom = load_rom(path)
    return rom, analyze(rom)


def inspect_cia_file(path: Path) -> CiaInfo:
    from .verify import inspect_cia

    return inspect_cia(path)

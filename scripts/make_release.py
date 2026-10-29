#!/usr/bin/env python3
"""Build the self-contained n64to3ds release archives for Linux and Windows.

Run on Linux x86_64 from a bootstrapped checkout (``scripts/bootstrap.sh
--from-image``): the pinned Daedalus tree with the patch applied and the
pinned devkitPro tree under third_party/.  Each archive contains the CLI, a
portable CPython, the patched Daedalus sources, devkitARM r66 with the 3DS
libraries, 3dstools and makerom, plus a recorded Daedalus build recipe
(tools/n64to3ds/kit.py).  Users need neither CMake nor a devkitPro install.

Every downloaded input is pinned by SHA-256.  The Windows devkitARM and
3dstools packages are fetched with devkitPro's own pacman (the package server
does not serve other clients); this needs unprivileged user namespaces
(``unshare -rm``).

With ``--check-rom`` the script also builds that ROM with CMake and with the
recipe in place, in all four variants, and requires byte-identical ELF, 3DSX
and CIA content; then it builds the ROM again from the extracted Linux archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.n64to3ds import __version__, builder, kit  # noqa: E402

DAEDALUS = ROOT / "third_party" / "daedalus"
DEVKITPRO = (ROOT / "third_party" / "devkitpro").resolve()
LINUX_MAKEROM = ROOT / "third_party" / "devkitpro-root" / "usr" / "local" / "bin" / "makerom"
PATCH = ROOT / "patches" / "daedalus-n64to3ds.patch"
PINNED_COMMIT = re.search(
    r'DAEDALUS_COMMIT="([0-9a-f]{40})"', (ROOT / "scripts" / "bootstrap.sh").read_text()
).group(1)
DEVKITARM_VERSION = "arm-none-eabi-gcc (devkitARM release 66) 15.1.0"

PYTHON_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/20250918/"
PYTHON = {
    "linux": (
        "cpython-3.12.11+20250918-x86_64-unknown-linux-gnu-install_only.tar.gz",
        "ece4001b4fe88f5fb0dea8a08d84853bad13ebb7077b862a654ea9f2e01f48e6",
    ),
    "windows": (
        "cpython-3.12.11+20250918-x86_64-pc-windows-msvc-install_only.tar.gz",
        "919257921a6c75bb8cec70ed2b3fe946f5be1395641ae8301154abf567a55cf5",
    ),
}
# Same versions as requirements.txt; abi3 wheels from PyPI.
WHEELS = {
    "linux": [
        (
            "rabbitizer-1.16.2-cp34-abi3-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl",
            "eafe91e07c9f400d2abc44c029c3b380cb68e65d2c32eb7e2015d391a51d2182",
        ),
        (
            "ipl3checksum-1.3.1-cp38-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
            "b0d23dd1811972a7351406a2b8752f22756a75e58cc70b345df47ce73056463b",
        ),
    ],
    "windows": [
        (
            "rabbitizer-1.16.2-cp34-abi3-win_amd64.whl",
            "3066c0484b9ea458a5b446ca41cf42b9ee9163f602a7796b699037f2aa9a8524",
        ),
        (
            "ipl3checksum-1.3.1-cp38-abi3-win_amd64.whl",
            "67715e57fc79c07dceef5ee900d60908638f6459be59b0e826981d49d8bbab0a",
        ),
    ],
}
MAKEROM_WINDOWS = (
    "https://github.com/3DSGuy/Project_CTR/releases/download/makerom-v0.18.4/makerom-v0.18.4-win_x86_64.zip",
    "d743507dc66d4c6f8efcce05ff8f5f47163a60e54e0e0c57fa12ec2f54acdf2f",
)
MAKEROM_LICENSE = (
    "https://raw.githubusercontent.com/3DSGuy/Project_CTR/makerom-v0.18.4/makerom/LICENSE",
    "6e0183da4f7ae7b58aa838db92f565a99ce3b1d0bb31adcf06aa30291665cdd8",
)
DKP_SERVER = "https://pkg.devkitpro.org/packages/"
# The pinned devkitPro image's versions (pacman/var/lib/pacman/local).
DKP_WINDOWS_PACKAGES = [
    ("windows/x86_64/devkitARM-r66-1-x86_64.pkg.tar.zst",
     "be0a5eb3435ac3988855048b7d1db0577e10a140ea79ec11edc7a47d65477779"),
    ("devkitarm-rules-1.6.0-4-any.pkg.tar.zst",
     "35023b2b5f07a2dcfb600063854c712206341fb5df3ffbaf8ff825d3cbc15fcc"),
    ("devkitarm-crtls-1.2.6-1-any.pkg.tar.zst",
     "65faf10f62c219a1a8a3a182b0c4c56da7992599035ea32b3d79e39e79f964bb"),
    ("windows/x86_64/3dstools-1.3.1-3-x86_64.pkg.tar.zst",
     "cdbfcaab36889442483a864b64a2d08be493a3e3e7432e158c0580b3e817f7d8"),
]

# devkitARM parts no build step uses: documentation, the debugger, lto-dump.
DEVKITARM_EXCLUDE = re.compile(r"^(share/|bin/arm-none-eabi-(gdb|lto-dump)[^/]*$)")
THREEDS_TOOLS = ("3dsxtool", "smdhtool", "mkromfs3ds")


def log(message: str) -> None:
    print(f"[release] {message}", flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, **kwargs)


def fetch(url: str, sha256: str, cache: Path, name: str | None = None) -> Path:
    path = cache / (name or url.rsplit("/", 1)[1])
    if not path.is_file() or sha256_file(path) != sha256:
        log(f"download {url}")
        cache.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as response, path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    actual = sha256_file(path)
    if actual != sha256:
        raise SystemExit(f"error: {path.name}: SHA-256 {actual}, expected {sha256}")
    return path


def fetch_wheel(filename: str, sha256: str, cache: Path) -> Path:
    path = cache / filename
    if path.is_file() and sha256_file(path) == sha256:
        return path
    project, version = filename.split("-")[:2]
    with urllib.request.urlopen(f"https://pypi.org/pypi/{project}/{version}/json", timeout=60) as response:
        files = json.load(response)["urls"]
    url = next(entry["url"] for entry in files if entry["filename"] == filename)
    return fetch(url, sha256, cache, filename)


# --- checkout checks ---------------------------------------------------------


def preflight() -> None:
    head = run(["git", "-C", str(DAEDALUS), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if head != PINNED_COMMIT:
        raise SystemExit(f"error: {DAEDALUS} is at {head}, expected {PINNED_COMMIT}")
    gcc = subprocess.run(
        [str(DEVKITPRO / "devkitARM" / "bin" / "arm-none-eabi-gcc"), "--version"],
        capture_output=True, text=True,
    ).stdout.splitlines()[:1]
    if gcc != [DEVKITARM_VERSION]:
        raise SystemExit(f"error: {DEVKITPRO} has {gcc}, expected {DEVKITARM_VERSION!r}")
    for path in (LINUX_MAKEROM, *(DEVKITPRO / "tools" / "bin" / name for name in THREEDS_TOOLS)):
        if not path.is_file():
            raise SystemExit(f"error: {path} is missing; run scripts/bootstrap.sh --from-image")
    if not shutil.which("cmake"):
        raise SystemExit("error: cmake is required to record the build recipe")


def daedalus_sources(dest: Path) -> None:
    """Pinned commit plus the patch, checked against the working tree.

    The recipe and the equivalence builds use the working tree, so it must be
    exactly what the archive ships.
    """

    dest = dest.resolve()
    dest.mkdir(parents=True)
    archive = run(["git", "-C", str(DAEDALUS), "archive", PINNED_COMMIT], capture_output=True).stdout
    run(["tar", "-x", "-C", str(dest)], input=archive)
    # dest lies inside this repository; stop git from applying relative to it.
    run(["git", "apply", str(PATCH)], cwd=dest, env={**os.environ, "GIT_CEILING_DIRECTORIES": str(dest.parent)})
    listed = run(
        ["git", "-C", str(DAEDALUS), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
    ).stdout.decode().split("\0")
    tree = {name for name in listed if name and (DAEDALUS / name).is_file()}
    shipped = {str(path.relative_to(dest)) for path in dest.rglob("*") if path.is_file()}
    if tree != shipped:
        raise SystemExit(
            "error: third_party/daedalus differs from the pinned commit + patch in its file set: "
            f"{sorted(tree ^ shipped)[:10]}"
        )
    for name in sorted(shipped):
        if (DAEDALUS / name).read_bytes() != (dest / name).read_bytes():
            raise SystemExit(f"error: third_party/daedalus/{name} differs from the pinned commit + patch")


# --- recipe --------------------------------------------------------------------


def record_recipe(work: Path) -> dict:
    toolchain = builder.discover_toolchain()
    stub = work / "aot" / "AOTGenerated.cpp"
    stub.parent.mkdir(parents=True)
    stub.write_text("// configure-only placeholder\n", encoding="utf-8")
    dummy = work / "post-build"
    variants = {}
    for aot, dualscreen in kit.ALL_VARIANTS:
        key = kit.variant_key(aot=aot, dualscreen=dualscreen)
        build_dir = work / key.replace(",", "-").replace("=", "-")
        command = builder.cmake_configure_command(
            cmake=toolchain.cmake,
            devkitpro=toolchain.devkitpro,
            build_dir=build_dir,
            romfs_name=kit.AUTOBOOT_SENTINEL,
            title_id_text=kit.TITLE_ID_SENTINEL,
            smdh=dummy / "app.smdh",
            banner=dummy / "banner.bnr",
            rsf=dummy / "app.rsf",
            romfs_dir=dummy / "romfs",
            dualscreen=dualscreen,
            ivfc_romfs=dummy / "romfs.bin",
            output_3dsx=dummy / "DaedalusX64.3dsx",
            output_cia=dummy / "DaedalusX64.cia",
            aot_source=stub if aot else None,
        ) + ["-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]
        run(command, env=toolchain.env, stdout=subprocess.DEVNULL)
        variants[key] = kit.recipe_from_cmake_build(
            build_dir,
            roots={"DEVKITPRO": toolchain.devkitpro, "DAEDALUS": builder.DAEDALUS_SOURCE, "RUNTIME": builder.RUNTIME_DIR},
            aot_source=stub if aot else None,
        )
        log(f"recorded {key}: {len(variants[key]['compile'])} compile steps")
    return {
        "format": kit.RECIPE_FORMAT,
        "n64to3ds_version": __version__,
        "daedalus_commit": PINNED_COMMIT,
        "daedalus_patch_sha256": sha256_file(PATCH),
        "devkitarm": DEVKITARM_VERSION,
        "recorded_with": toolchain.versions.get("cmake", "cmake"),
        "variants": variants,
    }


def _build_rom(
    rom: Path, output: Path, build_dir: Path, *, recipe: Path | None, aot: str, extensions: str
) -> dict[str, str]:
    """Build in this checkout, with CMake or (``recipe``) the recorded commands."""

    original = builder.discover_toolchain
    if recipe is not None:
        toolchain = original(require_host_compiler=aot == "auto")
        toolchain.kit_recipe = recipe
        toolchain.cmake = None
        builder.discover_toolchain = lambda **_: toolchain
    try:
        shutil.rmtree(build_dir, ignore_errors=True)
        builder.build(
            builder.BuildOptions(
                rom_path=rom, output_path=output, build_dir=build_dir, aot_mode=aot, extensions_mode=extensions
            )
        )
    finally:
        builder.discover_toolchain = original
    return {
        "elf": sha256_file(build_dir / "daedalus-build" / "Source" / "daedalus.elf"),
        "3dsx": sha256_file(output.with_suffix(".3dsx")),
        "cia_content": cia_content_sha256(output),
    }


def cia_content_sha256(path: Path) -> str:
    """Hash of the CIA content section.

    makerom fills the ticket and TMD signatures with random bytes, so two runs
    on identical inputs never produce identical CIA files.
    """

    data = path.read_bytes()
    header_size, _, _, cert_size, ticket_size, tmd_size, _, content_size = struct.unpack_from("<IHHIIIIQ", data)

    def align(value: int) -> int:
        return (value + 63) // 64 * 64

    content = align(align(align(align(header_size) + cert_size) + ticket_size) + tmd_size)
    return hashlib.sha256(data[content:content + content_size]).hexdigest()


def equivalence_check(rom: Path, recipe: Path, work: Path) -> dict[tuple[str, str], dict[str, str]]:
    """Same build directory, so even the debug info must match byte for byte.

    ``extensions=auto`` covers the dual-screen variants only for a ROM with a
    profile (Harvest Moon 64); ``off`` covers the others.
    """

    results = {}
    for aot in ("auto", "off"):
        for extensions in ("auto", "off"):
            build_dir = work / "equivalence-build"
            label = f"aot={aot} extensions={extensions}"
            via_cmake = _build_rom(
                rom, work / "cmake.cia", build_dir, recipe=None, aot=aot, extensions=extensions
            )
            via_recipe = _build_rom(
                rom, work / "recipe.cia", build_dir, recipe=recipe, aot=aot, extensions=extensions
            )
            log(f"equivalence {label}: cmake {via_cmake} recipe {via_recipe}")
            if via_cmake != via_recipe:
                raise SystemExit(f"error: the recipe build differs from the CMake build ({label})")
            results[(aot, extensions)] = via_recipe
    return results


# --- staging -------------------------------------------------------------------


def copy_tree(source: Path, dest: Path, *, exclude: re.Pattern[str] | None = None) -> None:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if exclude is not None and exclude.search(relative):
            continue
        target = dest / relative
        if path.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(path), target)
        elif path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def linked_portlibs(recipe: dict) -> set[str]:
    names = set()
    for variant in recipe["variants"].values():
        for token in variant["link"]["argv"]:
            if token.startswith("{DEVKITPRO}/portlibs/3ds/lib/"):
                names.add(token.rsplit("/", 1)[1])
            elif token.startswith("-l"):
                names.add(f"lib{token[2:]}.a")
    return {name for name in names if (DEVKITPRO / "portlibs" / "3ds" / "lib" / name).is_file()}


def stage_target_libraries(dest: Path, recipe: dict) -> None:
    """ARM libraries and headers; identical for both hosts."""

    copy_tree(DEVKITPRO / "libctru", dest / "libctru")
    copy_tree(DEVKITPRO / "picaGL", dest / "picaGL")
    copy_tree(DEVKITPRO / "portlibs" / "3ds" / "include", dest / "portlibs" / "3ds" / "include")
    libs = dest / "portlibs" / "3ds" / "lib"
    libs.mkdir(parents=True)
    for name in sorted(linked_portlibs(recipe)):
        shutil.copy2(DEVKITPRO / "portlibs" / "3ds" / "lib" / name, libs / name)


def fetch_windows_devkitpro(cache: Path) -> Path:
    root = cache / "devkitpro-windows-root"
    marker = root / ".complete"
    expected = "\n".join(sha for _, sha in DKP_WINDOWS_PACKAGES)
    if marker.is_file() and marker.read_text() == expected:
        return root / "opt" / "devkitpro"
    shutil.rmtree(root, ignore_errors=True)
    pacman_dir = DEVKITPRO / "pacman"
    work = cache / "pacman"
    shutil.rmtree(work, ignore_errors=True)
    for sub in ("db", "cache", "hooks"):
        (work / sub).mkdir(parents=True)
    root.mkdir(parents=True)
    (work / "pacman.conf").write_text(
        "[options]\nArchitecture = x86_64\nSigLevel = Required\nLocalFileSigLevel = Required\n",
        encoding="utf-8",
    )
    # dkp-pacman runs as root with its CA bundle at a compiled-in /opt path;
    # a private user and mount namespace provides both without privileges.
    script = work / "pacman.sh"
    script.write_text(
        "set -e\nmount -t tmpfs tmpfs /opt\n"
        "mkdir -p /opt/devkitpro/pacman/etc/ssl/certs\n"
        "cp /etc/ssl/certs/ca-certificates.crt /opt/devkitpro/pacman/etc/ssl/certs/cacert.pem\n"
        f'exec env LD_LIBRARY_PATH="{pacman_dir / "lib"}" "{pacman_dir / "bin" / "pacman"}" '
        f'--config "{work / "pacman.conf"}" --root "{root}" --dbpath "{work / "db"}" '
        f'--cachedir "{work / "cache"}" --logfile "{work / "pacman.log"}" --hookdir "{work / "hooks"}" '
        f'--gpgdir "{pacman_dir / "etc" / "pacman.d" / "gnupg"}" "$@"\n',
        encoding="utf-8",
    )
    urls = [DKP_SERVER + name for name, _ in DKP_WINDOWS_PACKAGES]
    log("fetching Windows devkitARM/3dstools with dkp-pacman")
    run(["unshare", "-rm", "bash", str(script), "-U", "--noconfirm", "-dd", "--noscriptlet", *urls])
    for name, sha in DKP_WINDOWS_PACKAGES:
        package = work / "cache" / name.rsplit("/", 1)[-1]
        if sha256_file(package) != sha:
            raise SystemExit(f"error: {package.name} does not match the pinned SHA-256")
    marker.write_text(expected)
    return root / "opt" / "devkitpro"


def stage_devkitpro(platform: str, dest: Path, recipe: dict, cache: Path) -> None:
    if platform == "linux":
        source = DEVKITPRO
        suffix = ""
    else:
        source = fetch_windows_devkitpro(cache)
        suffix = ".exe"
    copy_tree(source / "devkitARM", dest / "devkitARM", exclude=DEVKITARM_EXCLUDE)
    # The target libraries come from the pinned tree, so do their licenses.
    copy_tree(DEVKITPRO / "licenses", dest / "licenses")
    if source != DEVKITPRO:
        copy_tree(source / "licenses", dest / "licenses")
    stage_target_libraries(dest, recipe)
    tools = dest / "tools" / "bin"
    tools.mkdir(parents=True)
    for name in THREEDS_TOOLS:
        shutil.copy2(source / "tools" / "bin" / f"{name}{suffix}", tools / f"{name}{suffix}")
    if platform == "linux":
        shutil.copy2(LINUX_MAKEROM, tools / "makerom")
    else:
        with zipfile.ZipFile(fetch(*MAKEROM_WINDOWS, cache)) as archive:
            (tools / "makerom.exe").write_bytes(archive.read("makerom.exe"))
    shutil.copy2(fetch(*MAKEROM_LICENSE, cache, "makerom-LICENSE"), dest / "licenses" / "makerom-LICENSE.txt")


def stage_python(platform: str, dest: Path, cache: Path) -> None:
    name, sha = PYTHON[platform]
    with tarfile.open(fetch(PYTHON_URL + name.replace("+", "%2B"), sha, cache, name)) as archive:
        if hasattr(tarfile, "tar_filter"):
            archive.extractall(dest, filter="tar")
        else:
            archive.extractall(dest)
    python = dest / "python"
    site = python / ("Lib" if platform == "windows" else "lib/python3.12") / "site-packages"
    for test_dir in (python / "Lib" / "test", python / "lib" / "python3.12" / "test"):
        shutil.rmtree(test_dir, ignore_errors=True)
    for filename, wheel_sha in WHEELS[platform]:
        with zipfile.ZipFile(fetch_wheel(filename, wheel_sha, cache)) as wheel:
            wheel.extractall(site)


LINUX_LAUNCHER = """#!/bin/sh
# n64to3ds release launcher: the bundled Python, tools and devkitARM only.
here="$(cd "$(dirname "$0")" && pwd)"
exec "$here/python/bin/python3" -I "$here/n64to3ds.py" "$@"
"""

WINDOWS_LAUNCHER = """@echo off\r
rem n64to3ds release launcher: the bundled Python, tools and devkitARM only.\r
"%~dp0python\\python.exe" -I "%~dp0n64to3ds.py" %*\r
exit /b %ERRORLEVEL%\r
"""


def stage_bundle(platform: str, dest: Path, common: Path, recipe: dict, cache: Path) -> None:
    copy_tree(common, dest)
    stage_python(platform, dest, cache)
    stage_devkitpro(platform, dest / "third_party" / "devkitpro", recipe, cache)
    if platform == "linux":
        launcher = dest / "n64to3ds"
        launcher.write_text(LINUX_LAUNCHER, encoding="utf-8")
        launcher.chmod(0o755)
    else:
        (dest / "n64to3ds.cmd").write_bytes(WINDOWS_LAUNCHER.encode("ascii"))


def stage_common(dest: Path, recipe_path: Path, daedalus: Path) -> None:
    copy_tree(ROOT / "tools" / "n64to3ds", dest / "tools" / "n64to3ds")
    copy_tree(ROOT / "runtime" / "3ds", dest / "runtime" / "3ds")
    copy_tree(daedalus, dest / "third_party" / "daedalus")
    for relative in (
        "tests/host/aot_host.cpp",
        "patches/daedalus-n64to3ds.patch",
        "LICENSE",
        "NOTICE",
        "THIRD_PARTY_NOTICES.md",
    ):
        (dest / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, dest / relative)
    shutil.copy2(ROOT / "n64to3ds", dest / "n64to3ds.py")
    shutil.copy2(ROOT / "scripts" / "release" / "README.txt", dest / "README.txt")
    (dest / "kit").mkdir()
    shutil.copy2(recipe_path, dest / "kit" / "recipe.json")


# --- archives ------------------------------------------------------------------


def make_archive(platform: str, bundle: Path, out: Path) -> Path:
    name = bundle.name
    if platform == "linux":
        archive = out / f"{name}.tar.xz"
        run(
            ["tar", "--sort=name", "--owner=0", "--group=0", "--numeric-owner", "--mtime=@0",
             "-cJf", str(archive), "-C", str(bundle.parent), name],
            env={**os.environ, "XZ_OPT": "-6 -T0"},
        )
    else:
        archive = out / f"{name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as handle:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    info = zipfile.ZipInfo(f"{name}/{path.relative_to(bundle).as_posix()}", (1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    handle.writestr(info, path.read_bytes(), compresslevel=9)
    log(f"wrote {archive} ({archive.stat().st_size / 1e6:.1f} MB)")
    return archive


def smoke_test_linux(archive: Path, rom: Path, work: Path, reference: dict[str, str]) -> None:
    """Extract the Linux archive and build ``rom`` with a minimal environment.

    ``reference`` is the in-place recipe build with default options.  The ELF
    differs (debug info names the build directories), but the 3DSX and the
    CIA content carry no paths and must match.
    """

    extract = work / "extracted"
    shutil.rmtree(extract, ignore_errors=True)
    extract.mkdir(parents=True)
    run(["tar", "-xJf", str(archive), "-C", str(extract)])
    bundle = next(extract.iterdir())
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8"}
    run([str(bundle / "n64to3ds"), "doctor"], env=env)
    output = work / "smoke" / "game.cia"
    run([str(bundle / "n64to3ds"), "build", str(rom), "--output", str(output)], env=env)
    run([str(bundle / "n64to3ds"), "verify", str(output), "--rom", str(rom)], env=env)
    built = {"3dsx": sha256_file(output.with_suffix(".3dsx")), "cia_content": cia_content_sha256(output)}
    expected = {key: reference[key] for key in built}
    log(f"smoke test: bundle {built} in-place {expected}")
    if built != expected:
        raise SystemExit("error: the extracted bundle built a different 3DSX/CIA than the checkout")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform", choices=("linux", "windows", "all"), default="all")
    parser.add_argument("--out", type=Path, default=ROOT / "out" / "release")
    parser.add_argument("--cache", type=Path, default=ROOT / "build" / "release-cache")
    parser.add_argument("--check-rom", type=Path, help="ROM for the CMake/recipe equivalence and smoke builds")
    args = parser.parse_args()

    preflight()
    platforms = ("linux", "windows") if args.platform == "all" else (args.platform,)
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="n64to3ds-release-", dir=ROOT / "build") as temp:
        work = Path(temp)
        daedalus = work / "daedalus"
        daedalus_sources(daedalus)
        log("third_party/daedalus matches the pinned commit + patch")
        recipe = record_recipe(work / "recipe")
        recipe_path = work / "recipe.json"
        recipe_path.write_text(json.dumps(recipe, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        if args.check_rom:
            reference = equivalence_check(args.check_rom.resolve(), recipe_path, work)
        common = work / "common"
        stage_common(common, recipe_path, daedalus)
        archives = []
        for platform in platforms:
            bundle = work / "bundles" / platform / f"n64to3ds-{__version__}-{platform}-x86_64"
            stage_bundle(platform, bundle, common, recipe, args.cache)
            archives.append(make_archive(platform, bundle, args.out))
        if args.check_rom and "linux" in platforms:
            smoke_test_linux(
                args.out / f"n64to3ds-{__version__}-linux-x86_64.tar.xz",
                args.check_rom.resolve(),
                work,
                reference[("auto", "auto")],
            )
    sums = args.out / "SHA256SUMS.txt"
    sums.write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in sorted(args.out.glob("n64to3ds-*")) if path.is_file()),
        encoding="utf-8",
    )
    log(f"wrote {sums}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

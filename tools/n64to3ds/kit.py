"""Prerecorded Daedalus build recipe for the self-contained release bundles.

A source checkout builds Daedalus with CMake and devkitPro's CMake toolchain
files.  Those files refuse a native Windows CMake (they require msys2), so a
release bundle cannot use that path on Windows.  Instead the maintainer
records, on Linux, the exact compile and link commands that CMake generates
for every build variant, with machine-specific paths replaced by
placeholders.  The bundle replays those commands with its own devkitARM.  The
flags, sources and link order stay CMake's; only the driver differs, and the
release script checks that both drivers produce a byte-identical ELF.

The 3dsxtool and makerom post-build steps are not part of the recipe; the
builder runs them with the same arguments as the CMake custom commands.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import BuildError

RECIPE_FORMAT = 1

# Placeholders the builder fills in.  Paths are passed with forward slashes,
# which both devkitARM hosts accept.
PLACEHOLDERS = (
    "DEVKITPRO",
    "DAEDALUS",
    "RUNTIME",
    "BUILD",
    "AOT_SOURCE",
    "AUTOBOOT_ROM",
    "TITLE_ID",
)

# Configure-time values that stand in for the per-ROM defines.  They must not
# occur anywhere else in a generated command.
AUTOBOOT_SENTINEL = "N64TO3DSKITAUTOBOOT.z64"
TITLE_ID_SENTINEL = "N64TO3DSKITTITLEID"

# CMake names an out-of-tree source's object after its absolute path; the
# recipe gives the generated AOT translation unit and the runtime/3ds sources
# fixed object names instead.  Object names do not affect the linked ELF.
AOT_OBJECT = "CMakeFiles/daedalus.dir/n64to3ds-aot/AOTGenerated.o"
RUNTIME_OBJECT_DIR = "CMakeFiles/daedalus.dir/n64to3ds-runtime"

LINK_RESPONSE_FILE = "daedalus.elf.rsp"

_PLACEHOLDER_RE = re.compile(r"\{([A-Z_]+)\}")
_PATH_FLAG_PREFIXES = ("-I", "-L", "-o", "@", "-specs=")


def variant_key(*, aot: bool, dualscreen: bool) -> str:
    return f"aot={'on' if aot else 'off'},dualscreen={'on' if dualscreen else 'off'}"


ALL_VARIANTS = tuple((aot, dualscreen) for aot in (False, True) for dualscreen in (False, True))


def _relocate(text: str, roots: list[tuple[str, str]]) -> str:
    if "{" in text or "}" in text:
        raise BuildError(f"Recorded command token contains a brace: {text!r}")
    for name, value in roots:
        text = text.replace(value, "{" + name + "}")
    return text


def _check_relocated(token: str, forbidden: Iterable[str]) -> None:
    for value in forbidden:
        if value and value in token:
            raise BuildError(f"Recorded command still contains a local path: {token!r}")
    value = token
    for prefix in _PATH_FLAG_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if "=" in value:
        value = value.split("=", 1)[1]
    if value.startswith("/"):
        raise BuildError(f"Recorded command contains an absolute path: {token!r}")


def recipe_from_cmake_build(
    build_dir: Path,
    *,
    roots: dict[str, Path],
    aot_source: Path | None,
) -> dict[str, Any]:
    """Convert one configured CMake build tree into a relocatable variant.

    ``roots`` maps placeholder names (DEVKITPRO, DAEDALUS, RUNTIME) to the
    absolute paths used at configure time.  The build directory itself becomes
    ``{BUILD}``.
    """

    replacements = {name: str(path.resolve()) for name, path in roots.items()}
    replacements["BUILD"] = str(build_dir.resolve())
    if aot_source is not None:
        replacements["AOT_SOURCE"] = str(aot_source.resolve())
    ordered = sorted(replacements.items(), key=lambda item: len(item[1]), reverse=True)
    ordered += [("AUTOBOOT_ROM", AUTOBOOT_SENTINEL), ("TITLE_ID", TITLE_ID_SENTINEL)]
    forbidden = list(replacements.values()) + [str(Path.home())]

    source_dir = (build_dir / "Source").resolve()
    database = json.loads((build_dir / "compile_commands.json").read_text(encoding="utf-8"))
    compile_steps: list[dict[str, Any]] = []
    # Link-line name (relative to Source/) -> name used by the recipe.
    object_names: dict[str, str] = {}
    for entry in database:
        # Subdirectory object libraries compile in their own binary directory;
        # it is kept because it ends up in the debug info.
        directory = Path(entry["directory"]).resolve()
        if not directory.is_relative_to(source_dir):
            raise BuildError(f"Unexpected compile directory {directory} in {build_dir}.")
        argv = shlex.split(entry["command"])
        if argv.count("-o") != 1:
            raise BuildError(f"Unexpected compile command for {entry['file']}.")
        # "output" is only written by CMake 3.20+ for some generators.
        output = entry.get("output") or argv[argv.index("-o") + 1]
        link_name = os.path.relpath(directory / output, source_dir).replace(os.sep, "/")
        source = Path(entry["file"]).resolve()
        renamed = None
        if aot_source is not None and source == aot_source.resolve():
            renamed = AOT_OBJECT
        elif source.is_relative_to(roots["RUNTIME"].resolve()):
            relative = source.relative_to(roots["RUNTIME"].resolve()).with_suffix(".o")
            renamed = f"{RUNTIME_OBJECT_DIR}/{relative.as_posix()}"
        elif not source.is_relative_to(roots["DAEDALUS"].resolve()):
            raise BuildError(f"Unexpected source outside the Daedalus tree: {source}")
        if renamed is not None:
            if directory != source_dir:
                raise BuildError(f"Unexpected compile directory {directory} for {source}.")
            argv = [renamed if token == output else token for token in argv]
            object_names[link_name] = renamed
            output = renamed
        else:
            object_names[link_name] = link_name
        if argv.count(output) != 1 or "-c" not in argv:
            raise BuildError(f"Unexpected compile command for {source}.")
        if _relocate(output, ordered) != output:
            raise BuildError(f"Object path {output} depends on a local path.")
        relocated = [_relocate(token, ordered) for token in argv]
        for token in relocated:
            _check_relocated(token, forbidden)
        compile_steps.append(
            {
                "argv": relocated,
                "cwd": _relocate(str(directory), ordered),
                "output": output,
                "source": _relocate(str(source), ordered),
            }
        )

    link_text = (source_dir / "CMakeFiles" / "daedalus.dir" / "link.txt").read_text(encoding="utf-8")
    link_argv = shlex.split(link_text)
    linked_objects = [token for token in link_argv if token.endswith(".o")]
    if sorted(linked_objects) != sorted(object_names):
        raise BuildError("CMake's link line and compile database list different objects.")
    if link_argv.count("-o") != 1 or link_argv[link_argv.index("-o") + 1] != "daedalus.elf":
        raise BuildError("Unexpected CMake link output.")
    link_argv = [object_names.get(token, token) for token in link_argv]
    relocated_link = [_relocate(token, ordered) for token in link_argv]
    for token in relocated_link:
        _check_relocated(token, forbidden)
    return {
        "compile": compile_steps,
        "link": {"argv": relocated_link, "output": "daedalus.elf"},
    }


def load_recipe(path: Path) -> dict[str, Any]:
    try:
        recipe = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"Cannot read the build recipe {path}: {exc}") from exc
    if recipe.get("format") != RECIPE_FORMAT:
        raise BuildError(
            f"Unsupported build recipe format {recipe.get('format')!r} in {path}.",
            hint="The release bundle is incomplete or from another version; re-extract it.",
        )
    return recipe


def expand(text: str, values: dict[str, str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise BuildError(f"The build recipe needs a value for {{{name}}}.")
        return values[name]

    return _PLACEHOLDER_RE.sub(substitute, text)


def _program(path: str) -> str:
    if os.name == "nt" and not path.lower().endswith(".exe") and Path(path + ".exe").is_file():
        return path + ".exe"
    return path


def response_file_text(arguments: Iterable[str]) -> str:
    """Quote arguments for a GCC ``@file`` (libiberty ``buildargv`` rules)."""

    quoted = []
    for argument in arguments:
        escaped = argument.replace("\\", "\\\\").replace('"', '\\"')
        quoted.append(f'"{escaped}"')
    return "\n".join(quoted) + "\n"


def run_recipe(
    recipe: dict[str, Any],
    *,
    aot: bool,
    dualscreen: bool,
    values: dict[str, str],
    env: dict[str, str],
    jobs: int,
    log: Callable[[str], None],
    verbose: bool,
    run_link: Callable[[list[str], Path], None],
) -> Path:
    """Compile and link Daedalus for one variant; return the ELF path."""

    key = variant_key(aot=aot, dualscreen=dualscreen)
    variant = recipe.get("variants", {}).get(key)
    if variant is None:
        raise BuildError(f"The build recipe has no variant {key}.")
    cwd = Path(values["BUILD"]) / "Source"
    cwd.mkdir(parents=True, exist_ok=True)

    commands = []
    for step in variant["compile"]:
        argv = [expand(token, values) for token in step["argv"]]
        argv[0] = _program(argv[0])
        step_cwd = Path(expand(step["cwd"], values))
        (step_cwd / expand(step["output"], values)).parent.mkdir(parents=True, exist_ok=True)
        commands.append((argv, step_cwd))

    def compile_one(argv: list[str], step_cwd: Path) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                cwd=str(step_cwd),
                env=env,
                capture_output=True,
                text=True,
                errors="replace",
            )
        except OSError as exc:
            raise BuildError(f"Could not start {argv[0]}: {exc}") from exc

    log(f"\n== Daedalus compile ({key}, {len(commands)} files, {jobs} jobs)\n")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = [pool.submit(compile_one, argv, step_cwd) for argv, step_cwd in commands]
        try:
            for (argv, _), future in zip(commands, futures):
                result = future.result()
                line = f"$ {' '.join(argv)}\n"
                log(line + result.stdout + result.stderr)
                if verbose:
                    print(line + result.stdout + result.stderr, end="")
                if result.returncode != 0:
                    tail = "\n".join((result.stdout + result.stderr).splitlines()[-20:])
                    raise BuildError(
                        f"Compiling {argv[-1]} failed with exit code {result.returncode}.",
                        hint=f"Last output:\n{tail}",
                    )
        except BaseException:
            for future in futures:
                future.cancel()
            raise

    link = variant["link"]
    argv = [expand(token, values) for token in link["argv"]]
    response = cwd / LINK_RESPONSE_FILE
    response.write_text(response_file_text(argv[1:]), encoding="utf-8")
    run_link([_program(argv[0]), f"@{LINK_RESPONSE_FILE}"], cwd)
    elf = cwd / link["output"]
    if not elf.is_file():
        raise BuildError(f"Linking finished but {elf} was not created.")
    return elf

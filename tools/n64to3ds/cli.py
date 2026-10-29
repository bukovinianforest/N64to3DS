"""Command-line interface for n64to3ds."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from . import __version__
from .builder import BuildOptions, analyze_rom, build, discover_toolchain
from .extensions import EXTENSIONS
from .errors import N64To3DSError
from .rom import load_rom
from .runtime_evidence import record_runtime
from .titleid import choose_title_id, format_title_id, parse_title_id
from .verify import inspect_cia, verify_cia


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="n64to3ds",
        description=(
            "Build a standalone Old 3DS CIA from an N64 ROM using the "
            "DaedalusX64 runtime, with optional conservative AOT compilation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  n64to3ds build "Harvest Moon 64.z64" --target old3ds --output "Harvest Moon 64.cia"\n'
            '  n64to3ds build game.v64 --aot off --output game.cia --keep-build\n'
            "  n64to3ds analyze game.z64 --json\n"
            "  n64to3ds verify game.cia --rom game.z64\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"n64to3ds {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser(
        "build",
        help="normalize a ROM and package it into a CIA",
        description=(
            "Normalize an N64 ROM, embed it in RomFS, and build a standalone "
            "Old 3DS CIA.  The ROM is opened from RomFS at startup; no ROM "
            "browser or external ROM file is used."
        ),
    )
    build_parser.add_argument("rom", type=Path, help="input .z64/.v64/.n64 ROM")
    build_parser.add_argument(
        "--target",
        default="old3ds",
        choices=["old3ds"],
        help="target hardware (only old3ds is supported)",
    )
    build_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output .cia path (default: <ROM stem>.cia)",
    )
    build_parser.add_argument(
        "--aot",
        choices=["off", "auto"],
        default="auto",
        help="AOT strategy: off uses only Daedalus, auto compiles proven blocks (default)",
    )
    build_parser.add_argument(
        "--extensions",
        default="auto",
        metavar="auto|off|ID",
        help="dual-screen bottom-screen extensions: auto uses a verified profile for this exact ROM "
        "if one exists (default), off disables them, or name one (" + ", ".join(sorted(EXTENSIONS)) + ")",
    )
    build_parser.add_argument(
        "-v", "--verbose", action="store_true", help="stream build commands and tool output"
    )
    build_parser.add_argument(
        "--keep-build", action="store_true", help="keep the intermediate build directory"
    )
    build_parser.add_argument(
        "--build-dir", type=Path, default=None, help="use a specific intermediate build directory"
    )
    build_parser.add_argument(
        "--title-id",
        default=None,
        help="explicit title id (16/8 hex digits or a 24-bit unique id)",
    )
    build_parser.add_argument(
        "--app-name",
        default=None,
        help="HOME menu name (default: the ROM header name); with --title-id, lets two builds of one ROM coexist",
    )
    build_parser.add_argument(
        "--jobs", type=_positive_int, default=None, help="parallel build jobs (default: CPU count)"
    )
    build_parser.add_argument(
        "--rom-db", type=Path, default=None, help="custom Daedalus roms.ini database"
    )
    build_parser.add_argument(
        "--save-type",
        default="auto",
        help="override save type: auto, Eeprom4k, Eeprom16k, SRAM, FlashRam, Unknown",
    )
    build_parser.add_argument(
        "--no-registry",
        action="store_true",
        help="do not read/write the local title-id collision registry",
    )

    analyze_parser = subparsers.add_parser(
        "analyze",
        help="analyze a ROM and report safe AOT coverage",
        description=(
            "Run the conservative MIPS analyzer.  This never executes the ROM; "
            "it maps the statically loaded segment and follows proven control flow."
        ),
    )
    analyze_parser.add_argument("rom", type=Path, help="input .z64/.v64/.n64 ROM")
    analyze_parser.add_argument("--output", "-o", type=Path, default=None, help="write full JSON report")
    analyze_parser.add_argument("--json", action="store_true", help="print the full JSON report")
    analyze_parser.add_argument(
        "--max-blocks", type=_positive_int, default=4096, help="maximum selected AOT blocks"
    )
    analyze_parser.add_argument(
        "--max-instructions",
        type=_positive_int,
        default=16384,
        help="maximum selected AOT instructions",
    )
    analyze_parser.add_argument(
        "--hypotheses",
        type=_positive_int,
        default=2,
        help="maximum ROM-to-RDRAM mapping hypotheses to analyze (default: 2)",
    )

    verify_parser = subparsers.add_parser(
        "verify",
        help="inspect a CIA and verify its embedded ROM",
        description=(
            "Parse the CIA/NCCH/RomFS structures, validate the IVFC hashes, and "
            "compare the embedded ROM with --rom when supplied."
        ),
    )
    verify_parser.add_argument("cia", type=Path, help="CIA to inspect")
    verify_parser.add_argument("--rom", type=Path, default=None, help="expected normalized input ROM")
    verify_parser.add_argument(
        "--title-id", default=None, help="expected title id (otherwise read from the manifest or derive it)"
    )
    verify_parser.add_argument("--json", action="store_true", help="print the full JSON result")

    record_parser = subparsers.add_parser(
        "record-runtime",
        help="record emulator/AOT evidence from runtime logs into a manifest",
        description=(
            "Parse the logs written by a Daedalus run and update only the status "
            "claims they can support.  A rendered first frame sets 'emulator boot "
            "verified'; an executed generated block whose log ROM SHA-256 matches "
            "the manifest sets 'AOT exercised'.  Playability and full speed are "
            "never inferred from these logs."
        ),
    )
    record_parser.add_argument("manifest", type=Path, help="<output>.cia.manifest.json")
    record_parser.add_argument(
        "--aot-log", type=Path, default=None, help="n64to3ds-aot.log from the AOT run"
    )
    record_parser.add_argument(
        "--daedalus-log", type=Path, default=None, help="daedalus.log from the same run"
    )
    record_parser.add_argument(
        "--no-copy-logs",
        action="store_true",
        help="record log paths without copying the logs next to the manifest",
    )
    record_parser.add_argument("--json", action="store_true", help="print the evidence as JSON")

    doctor_parser = subparsers.add_parser(
        "doctor", help="check the host toolchain and pinned Daedalus tree"
    )
    doctor_parser.add_argument("--verbose", action="store_true")

    subparsers.add_parser("version", help="print the tool version")
    return parser


def _default_output(rom_path: Path) -> Path:
    return Path.cwd() / f"{rom_path.stem}.cia"


def _cmd_build(args: argparse.Namespace) -> int:
    output = args.output.expanduser() if args.output else _default_output(args.rom)
    options = BuildOptions(
        rom_path=args.rom,
        output_path=output,
        target=args.target,
        aot_mode=args.aot,
        verbose=args.verbose,
        keep_build=args.keep_build,
        build_dir=args.build_dir,
        title_id=args.title_id,
        app_name=args.app_name,
        jobs=args.jobs or BuildOptions.__dataclass_fields__["jobs"].default,
        rom_db=args.rom_db,
        save_type=args.save_type,
        use_registry=not args.no_registry,
        extensions_mode=args.extensions,
    )
    manifest = build(options)
    print()
    print(f"ROM:        {manifest.rom.get('source_path')}")
    print(f"SHA-256:    {manifest.rom.get('sha256')}")
    print(f"Title ID:   {manifest.title.get('title_id')}")
    aot = manifest.aot
    print(
        f"AOT:        {aot.get('mode')} "
        f"({aot.get('blocks', 0)} blocks, {aot.get('instructions', 0)} instructions)"
    )
    host_test = aot.get("host_semantic_test", {})
    if host_test.get("ran"):
        print(
            "AOT test:   host semantic harness passed "
            f"({host_test.get('executions', 0)} block executions)"
        )
    elif aot.get("mode") == "auto":
        print(f"AOT test:   not run ({host_test.get('reason', 'unknown reason')})")
    extension = manifest.extensions.get("extension")
    print(
        f"Extension:  {extension} (profile {manifest.extensions.get('profile')})"
        if extension
        else f"Extension:  none (--extensions {manifest.extensions.get('mode', 'auto')})"
    )
    cia = manifest.artifacts.get("cia") or {}
    print(f"CIA:        {cia.get('path')} ({cia.get('size', 0)} bytes)")
    print(f"Manifest:   {manifest.artifacts.get('manifest', {}).get('path')}")
    print(f"Build log:  {manifest.log_path}")
    print()
    manifest.print_status()
    if aot.get("mode") == "auto":
        print(
            "\nNote: the host semantic harness executes the generated blocks against an "
            "independent MIPS reference interpreter, but on-device AOT execution still "
            "requires a real Old 3DS or a 3DS emulator run."
        )
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    from .analyzer import analyze_multi

    rom = load_rom(args.rom)
    result = analyze_multi(
        rom,
        max_hypotheses=args.hypotheses,
        max_blocks=args.max_blocks,
        max_instructions=args.max_instructions,
    )
    report = result.as_dict()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        stats = report["stats"]
        mapping = report["mapping"]
        print(f"ROM:          {rom.source_path}")
        print(f"SHA-256:      {rom.sha256}")
        print(f"Name:         {rom.display_name}")
        print(f"Entry:        {rom.header.entry_point:#010x}")
        mapping_base = int(mapping["base_vaddr"], 16)
        print(f"Mapping:      {mapping['base_vaddr']} .. 0x{mapping_base + mapping['size']:08X}")
        print(f"Candidates:   {stats['candidate_blocks']}")
        print(f"Safe blocks:  {stats['safe_blocks']}")
        print(f"Selected:     {stats['selected_blocks']} blocks, {stats['selected_instructions']} instructions")
        if report.get("hypotheses"):
            print("Hypotheses:")
            for hypothesis in report["hypotheses"]:
                print(
                    f"  - {hypothesis['base_vaddr']}: score {hypothesis['score']}, "
                    f"{hypothesis['selected_blocks']} blocks / "
                    f"{hypothesis['selected_instructions']} instructions"
                )
        if stats.get("unsupported_counts"):
            print("Unsupported:  " + ", ".join(
                f"{key}={value}" for key, value in stats["unsupported_counts"].items()
            ))
        if report.get("warnings"):
            print("Warnings:")
            for warning in report["warnings"]:
                print(f"  - {warning}")
        if args.output:
            print(f"Report:       {args.output}")
    return 0


def _expected_title_id(cia_path: Path, explicit: str | None) -> int | None:
    if explicit:
        return parse_title_id(explicit)[0]
    manifest_path = cia_path.with_name(cia_path.name + ".manifest.json")
    if manifest_path.is_file():
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            text = value.get("title", {}).get("title_id")
            if text:
                return parse_title_id(text)[0]
        except (OSError, ValueError, KeyError):
            pass
    return None


def _cmd_verify(args: argparse.Namespace) -> int:
    if args.rom is None:
        info = inspect_cia(args.cia)
        expected = _expected_title_id(args.cia, args.title_id)
        errors: list[str] = []
        if expected is not None and info.title_id != expected:
            errors.append(
                f"NCCH title id is {info.title_id:016X}, expected {expected:016X}."
            )
        if args.json:
            payload = info.as_dict()
            payload["verification"] = {"ok": not errors, "errors": errors}
            print(json.dumps(payload, indent=2))
        else:
            print(f"CIA:          {info.path}")
            print(f"Size:         {info.file_size} bytes")
            print(f"Title ID:     {info.title_id:016X}")
            print(f"Product code: {info.product_code}")
            print(f"RomFS:        {info.romfs_size} bytes at 0x{info.romfs_offset:X}")
            print(f"ROM file:     {info.rom_file_path} ({info.rom_size} bytes)")
            print(f"ROM SHA-256:  {info.rom_sha256}")
            print("IVFC hashes:  valid")
            print("Files:        " + ", ".join(name for name, _ in info.files))
            print(f"Verification: {'PASS' if not errors else 'FAIL'}")
            for error in errors:
                print(f"  - {error}")
        return 0 if not errors else 1

    rom = load_rom(args.rom)
    expected = _expected_title_id(args.cia, args.title_id)
    if expected is None:
        expected, _unique, _warnings = choose_title_id(
            rom.sha256, rom.display_name, use_registry=False
        )
    result = verify_cia(args.cia, rom, expected)
    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(f"CIA:          {result.cia.path}")
        print(f"Title ID:     {result.cia.title_id:016X}")
        print(f"ROM file:     {result.cia.rom_file_path}")
        print(f"ROM SHA-256:  {result.cia.rom_sha256}")
        print("IVFC hashes:  valid")
        print(f"Verification: {'PASS' if result.ok else 'FAIL'}")
        for error in result.errors:
            print(f"  - {error}")
    return 0 if result.ok else 1


def _cmd_record_runtime(args: argparse.Namespace) -> int:
    result = record_runtime(
        args.manifest,
        aot_log=args.aot_log,
        daedalus_log=args.daedalus_log,
        copy_logs=not args.no_copy_logs,
    )
    if args.json:
        print(json.dumps(result.evidence, indent=2))
    else:
        print(f"Manifest:    {result.manifest_path}")
        aot = result.evidence.get("aot_log")
        if aot:
            print(
                "AOT log:     "
                f"{aot.get('path')} "
                f"(blocks={aot.get('block_executions', 0)}, "
                f"instructions={aot.get('instruction_executions', 0)})"
            )
        daedalus = result.evidence.get("daedalus_log")
        if daedalus:
            print(
                "Daedalus:    "
                f"{daedalus.get('path')} "
                f"(first frame pc={daedalus.get('first_frame_pc')})"
            )
        print()
        print(f"emulator boot verified: {'yes' if result.boot_verified else 'no'}")
        print(f"AOT exercised: {'yes' if result.aot_exercised else 'no'}")
        print("game playable: no (not provable from these logs)")
        print("full speed: no (not provable from these logs)")
        print("hardware-unverified")
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        for error in result.errors:
            print(f"error: {error}", file=sys.stderr)
    return 0 if result.ok else 1


def _cmd_doctor(args: argparse.Namespace) -> int:
    toolchain = discover_toolchain(require_host_compiler=False)
    print("n64to3ds doctor")
    print(f"DEVKITPRO:  {toolchain.devkitpro}")
    print(f"DEVKITARM:  {toolchain.devkitarm}")
    print(f"picaGL:     {toolchain.devkitpro / 'picaGL'}")
    for name, value in toolchain.versions.items():
        print(f"{name}: {value}")
    from .builder import DAEDALUS_ROOT, RUNTIME_DIR, _daedalus_commit

    print(f"Daedalus:   {DAEDALUS_ROOT}")
    commit = _daedalus_commit()
    print(f"Commit:     {commit or 'not a git checkout'}")
    print(f"Runtime:    {RUNTIME_DIR}")
    missing = [
        str(path)
        for path in (
            DAEDALUS_ROOT / "Source" / "SysCTR" / "Resources" / "banner.bnr",
            DAEDALUS_ROOT / "Source" / "SysCTR" / "Resources" / "icon.png",
            DAEDALUS_ROOT / "Source" / "SysCTR" / "Resources" / "template.rsf",
            RUNTIME_DIR / "AOT" / "AOTRuntime.cpp",
            RUNTIME_DIR / "AOT" / "AOTGeneratedCommon.h",
        )
        if not path.is_file()
    ]
    if missing:
        print("Missing required files:")
        for path in missing:
            print(f"  - {path}")
        return 1
    print("Toolchain check: PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            return _cmd_build(args)
        if args.command == "analyze":
            return _cmd_analyze(args)
        if args.command == "verify":
            return _cmd_verify(args)
        if args.command == "record-runtime":
            return _cmd_record_runtime(args)
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "version":
            print(f"n64to3ds {__version__}")
            return 0
        parser.error(f"unknown command {args.command!r}")
    except N64To3DSError as exc:
        print(f"error: {exc.format()}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover - unexpected failures
        if getattr(args, "verbose", False):
            traceback.print_exc()
        else:
            print(f"error: {exc}", file=sys.stderr)
            print("Hint: re-run with --verbose for a traceback.", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

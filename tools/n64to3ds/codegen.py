"""Generate C++ AOT block functions from conservative analyzer output.

The generated functions use the macros in ``AOTGeneratedCommon.h`` so the same
code can be compiled into Daedalus (where the macros address ``gGPR``,
``gCPUState`` and Daedalus' memory helpers) and into the host semantic test
harness (where the macros address a small test machine).

The generator deliberately mirrors Daedalus' interpreter semantics in
``Source/Core/R4300.cpp`` instead of inventing a "cleaner" MIPS model.  In
particular, 32-bit arithmetic wraps and is sign-extended into the 64-bit GPR,
64-bit comparisons use the full GPR, and the interpreter's non-trapping ADD /
ADDI behaviour is preserved.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .analyzer import BasicBlock, InstructionInfo
from .errors import BuildError


AOT_STATUS_OK = 0
AOT_STATUS_STOP = 1

FNV_OFFSET = 1469598103934665603
FNV_PRIME = 1099511628211


@dataclass(frozen=True)
class GeneratedBlock:
    start: int
    name: str
    instruction_count: int
    fingerprint: int
    source: str
    words: tuple[int, ...]
    mapping_base: int = 0
    source_offset: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "start": f"0x{self.start:08X}",
            "name": self.name,
            "instructions": self.instruction_count,
            "fingerprint": f"0x{self.fingerprint:016X}",
            "mapping_base": f"0x{self.mapping_base:08X}",
            "source_offset": f"0x{self.source_offset:08X}",
            "words": [f"0x{word:08X}" for word in self.words],
        }


@dataclass(frozen=True)
class ProgramMetadata:
    """Identity of the ROM/mapping the generated blocks were proven against."""

    rom_sha256: str = ""
    mapping_base: int = 0
    mapped_size: int = 0
    game_name: str = ""
    aot_mode: str = "auto"
    hypotheses: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "rom_sha256": self.rom_sha256,
            "mapping_base": f"0x{self.mapping_base:08X}",
            "mapped_size": self.mapped_size,
            "game_name": self.game_name,
            "aot_mode": self.aot_mode,
            "hypotheses": [dict(item) for item in self.hypotheses],
        }


@dataclass
class GeneratedProgram:
    blocks: list[GeneratedBlock]
    source: str
    metadata: ProgramMetadata = field(default_factory=ProgramMetadata)

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def instruction_count(self) -> int:
        return sum(block.instruction_count for block in self.blocks)

    def as_dict(self) -> dict[str, object]:
        return {
            "metadata": self.metadata.as_dict(),
            "block_count": self.block_count,
            "instruction_count": self.instruction_count,
            "blocks": [block.as_dict() for block in self.blocks],
        }


def fingerprint_words(words: Iterable[int]) -> int:
    value = FNV_OFFSET
    for word in words:
        for shift in (24, 16, 8, 0):
            value ^= (word >> shift) & 0xFF
            value = (value * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return value


def _u32(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08X}u"


def _s16(imm: int) -> str:
    return f"(s32)(s16){imm & 0xFFFF}"


def _escape_c_string(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _addr(insn: InstructionInfo) -> str:
    # Daedalus forms the address with 32-bit wrapping.  Use unsigned
    # arithmetic so C++ signed overflow is not invoked.
    return f"(u32)(AOT_GPR_U32({insn.rs}) + (u32){_s16(insn.imm)})"


def _gpr_u64(reg: int) -> str:
    return f"AOT_GPR_U64({reg})"


def _gpr_s64(reg: int) -> str:
    return f"AOT_GPR_S64({reg})"


def _gpr_u32(reg: int) -> str:
    return f"AOT_GPR_U32({reg})"


def _gpr_s32(reg: int) -> str:
    return f"AOT_GPR_S32({reg})"


def _set_gpr_u64(reg: int, expression: str) -> list[str]:
    if reg == 0:
        return []
    return [f"AOT_GPR_U64({reg}) = (u64)({expression});"]


def _set_gpr_s64(reg: int, expression: str) -> list[str]:
    if reg == 0:
        return []
    return [f"AOT_GPR_U64({reg}) = (u64)(s64)({expression});"]


def _next_pc_expr(next_pc: int | str) -> str:
    if isinstance(next_pc, str):
        return next_pc
    return _u32(next_pc)


def _mem_check(executed: int, next_pc: int | str) -> str:
    return f"AOT_CHECK_JOB({executed}, {_next_pc_expr(next_pc)});"


def _memory_instruction(
    insn: InstructionInfo,
    executed: int,
    next_pc: int | str,
    delay_slot: bool = False,
) -> list[str]:
    """Emit a load/store and stop the block if Daedalus queued a job.

    ``AOT_SET_PC`` is required before the access because Daedalus' TLB
    exception handlers record ``gCPUState.CurrentPC`` as the faulting
    instruction.  A memory access in a branch delay slot also has to expose
    ``Delay == EXEC_DELAY`` so a TLB exception records ``CAUSE_BD`` and the
    branch EPC exactly as the interpreter does.
    """

    pc = insn.vaddr
    lines = [f"AOT_SET_PC({_u32(pc)});"]
    if delay_slot:
        lines.append("AOT_SET_DELAY_EXEC();")
    addr = _addr(insn)
    rt = insn.rt
    if insn.kind == "lb":
        lines += _set_gpr_s64(rt, f"(s8)AOT_READ8({addr})")
    elif insn.kind == "lbu":
        lines += _set_gpr_u64(rt, f"(u8)AOT_READ8({addr})")
    elif insn.kind == "lh":
        lines += _set_gpr_s64(rt, f"(s16)AOT_READ16({addr})")
    elif insn.kind == "lhu":
        lines += _set_gpr_u64(rt, f"(u16)AOT_READ16({addr})")
    elif insn.kind == "lw":
        lines += _set_gpr_s64(rt, f"(s32)AOT_READ32({addr})")
    elif insn.kind == "lwu":
        lines += _set_gpr_u64(rt, f"(u32)AOT_READ32({addr})")
    elif insn.kind == "ld":
        lines += _set_gpr_u64(rt, f"AOT_READ64({addr})")
    elif insn.kind == "sb":
        lines += [f"AOT_WRITE8({addr}, (u8){_gpr_u32(rt)});"]
    elif insn.kind == "sh":
        lines += [f"AOT_WRITE16({addr}, (u16){_gpr_u32(rt)});"]
    elif insn.kind == "sw":
        lines += [f"AOT_WRITE32({addr}, {_gpr_u32(rt)});"]
    elif insn.kind == "sd":
        lines += [f"AOT_WRITE64({addr}, {_gpr_u64(rt)});"]
    else:  # pragma: no cover - the analyzer filters unsupported memory ops
        raise BuildError(f"internal error: unsupported memory op {insn.kind} at 0x{pc:08X}")
    lines.append(_mem_check(executed, next_pc))
    return lines


def emit_instruction(
    insn: InstructionInfo,
    executed: int,
    next_pc: int | str,
    delay_slot: bool = False,
) -> list[str]:
    """Emit one non-control-flow instruction.

    ``executed`` is the number of instructions executed including this one;
    it is used by the early-stop path.  ``next_pc`` is where the CPU should
    continue if the instruction queues a job.  ``delay_slot`` is true only for
    the instruction in a branch/jump delay slot.
    """

    kind = insn.kind
    rs, rt, rd, sa, imm = insn.rs, insn.rt, insn.rd, insn.sa, insn.imm

    if kind in ("nop", "sync"):
        return []
    if kind in ("lb", "lbu", "lh", "lhu", "lw", "lwu", "ld", "sb", "sh", "sw", "sd"):
        return _memory_instruction(insn, executed, next_pc, delay_slot)

    if kind == "lui":
        return _set_gpr_s64(rt, f"(s32)((u32)(s32)(s16){imm & 0xFFFF} << 16)")
    if kind in ("addi", "addiu"):
        return _set_gpr_s64(rt, f"(s32)({_gpr_u32(rs)} + (u32){_s16(imm)})")
    if kind in ("daddi", "daddiu"):
        return _set_gpr_s64(rt, f"(s64)({_gpr_u64(rs)} + (u64)(s64){_s16(imm)})")
    if kind == "slti":
        return _set_gpr_u64(rt, f"({_gpr_s64(rs)} < (s64){_s16(imm)}) ? 1 : 0")
    if kind == "sltiu":
        return _set_gpr_u64(rt, f"({_gpr_u64(rs)} < (u64)(s64){_s16(imm)}) ? 1 : 0")
    if kind == "andi":
        return _set_gpr_u64(rt, f"{_gpr_u64(rs)} & (u64)(u16){imm & 0xFFFF}")
    if kind == "ori":
        return _set_gpr_u64(rt, f"{_gpr_u64(rs)} | (u64)(u16){imm & 0xFFFF}")
    if kind == "xori":
        return _set_gpr_u64(rt, f"{_gpr_u64(rs)} ^ (u64)(u16){imm & 0xFFFF}")

    if kind == "sll":
        return _set_gpr_s64(rd, f"(s32)(({_gpr_u32(rt)} << {sa}) & 0xFFFFFFFFu)")
    if kind == "srl":
        return _set_gpr_s64(rd, f"(s32)({_gpr_u32(rt)} >> {sa})")
    if kind == "sra":
        return _set_gpr_s64(rd, f"AOT_SRA32({_gpr_s32(rt)}, {sa})")
    if kind == "sllv":
        return _set_gpr_s64(rd, f"(s32)(({_gpr_u32(rt)} << ({_gpr_u32(rs)} & 31u)) & 0xFFFFFFFFu)")
    if kind == "srlv":
        return _set_gpr_s64(rd, f"(s32)({_gpr_u32(rt)} >> ({_gpr_u32(rs)} & 31u))")
    if kind == "srav":
        return _set_gpr_s64(rd, f"AOT_SRA32({_gpr_s32(rt)}, {_gpr_u32(rs)} & 31u)")

    if kind in ("add", "addu"):
        return _set_gpr_s64(rd, f"(s32)({_gpr_u32(rs)} + {_gpr_u32(rt)})")
    if kind in ("sub", "subu"):
        return _set_gpr_s64(rd, f"(s32)({_gpr_u32(rs)} - {_gpr_u32(rt)})")
    if kind == "and":
        return _set_gpr_u64(rd, f"{_gpr_u64(rs)} & {_gpr_u64(rt)}")
    if kind == "or":
        return _set_gpr_u64(rd, f"{_gpr_u64(rs)} | {_gpr_u64(rt)}")
    if kind == "xor":
        return _set_gpr_u64(rd, f"{_gpr_u64(rs)} ^ {_gpr_u64(rt)}")
    if kind == "nor":
        return _set_gpr_u64(rd, f"~({_gpr_u64(rs)} | {_gpr_u64(rt)})")
    if kind == "slt":
        return _set_gpr_u64(rd, f"({_gpr_s64(rs)} < {_gpr_s64(rt)}) ? 1 : 0")
    if kind == "sltu":
        return _set_gpr_u64(rd, f"({_gpr_u64(rs)} < {_gpr_u64(rt)}) ? 1 : 0")

    if kind == "dadd":
        return _set_gpr_s64(rd, f"(s64)({_gpr_u64(rs)} + {_gpr_u64(rt)})")
    if kind == "daddu":
        return _set_gpr_u64(rd, f"{_gpr_u64(rs)} + {_gpr_u64(rt)}")
    if kind == "dsub":
        return _set_gpr_s64(rd, f"(s64)({_gpr_u64(rs)} - {_gpr_u64(rt)})")
    if kind == "dsubu":
        return _set_gpr_u64(rd, f"{_gpr_u64(rs)} - {_gpr_u64(rt)}")
    if kind == "dsll":
        return _set_gpr_u64(rd, f"{_gpr_u64(rt)} << {sa}")
    if kind == "dsrl":
        return _set_gpr_u64(rd, f"{_gpr_u64(rt)} >> {sa}")
    if kind == "dsra":
        return _set_gpr_u64(rd, f"(u64)AOT_SRA64({_gpr_s64(rt)}, {sa})")
    if kind == "dsll32":
        return _set_gpr_u64(rd, f"{_gpr_u64(rt)} << {32 + sa}")
    if kind == "dsrl32":
        return _set_gpr_u64(rd, f"{_gpr_u64(rt)} >> {32 + sa}")
    if kind == "dsra32":
        return _set_gpr_u64(rd, f"(u64)AOT_SRA64({_gpr_s64(rt)}, {32 + sa})")

    if kind == "mfhi":
        return _set_gpr_u64(rd, "AOT_HI_U64()")
    if kind == "mflo":
        return _set_gpr_u64(rd, "AOT_LO_U64()")
    if kind == "mthi":
        return [f"AOT_SET_HI_U64({_gpr_u64(rs)});"]
    if kind == "mtlo":
        return [f"AOT_SET_LO_U64({_gpr_u64(rs)});"]

    if kind == "mult":
        return [
            "{",
            f"  const s64 aot_product = (s64){_gpr_s32(rs)} * (s64){_gpr_s32(rt)};",
            f"  AOT_SET_LO_U64((u64)(s64)(s32)(aot_product & 0xFFFFFFFFll));",
            f"  AOT_SET_HI_U64((u64)(s64)(s32)(aot_product >> 32));",
            "}",
        ]
    if kind == "multu":
        return [
            "{",
            f"  const u64 aot_product = (u64){_gpr_u32(rs)} * (u64){_gpr_u32(rt)};",
            "  AOT_SET_LO_U64((u64)(s64)(s32)(aot_product & 0xFFFFFFFFull));",
            "  AOT_SET_HI_U64((u64)(s64)(s32)(aot_product >> 32));",
            "}",
        ]
    if kind in ("div", "divu"):
        helper = "AOT_DIV32" if kind == "div" else "AOT_DIVU32"
        return [
            "{",
            f"  const AOTDivResult aot_div = {helper}({_gpr_s32(rs) if kind == 'div' else _gpr_u32(rs)}, "
            f"{_gpr_s32(rt) if kind == 'div' else _gpr_u32(rt)});",
            "  if (aot_div.changed) {",
            "    AOT_SET_LO_U64(aot_div.lo);",
            "    AOT_SET_HI_U64(aot_div.hi);",
            "  }",
            "}",
        ]

    raise BuildError(
        f"internal error: no AOT emitter for {kind} at 0x{insn.vaddr:08X}"
    )


def _branch_condition(insn: InstructionInfo) -> str:
    rs, rt = insn.rs, insn.rt
    if insn.kind == "beq":
        return f"({_gpr_u64(rs)} == {_gpr_u64(rt)})"
    if insn.kind == "bne":
        return f"({_gpr_u64(rs)} != {_gpr_u64(rt)})"
    if insn.kind == "blez":
        return f"({_gpr_s64(rs)} <= 0)"
    if insn.kind == "bgtz":
        return f"({_gpr_s64(rs)} > 0)"
    if insn.kind in ("bltz", "bltzal"):
        return f"({_gpr_s64(rs)} < 0)"
    if insn.kind in ("bgez", "bgezal"):
        return f"({_gpr_s64(rs)} >= 0)"
    raise BuildError(f"internal error: no branch condition for {insn.kind}")


def _link_ra(pc: int) -> list[str]:
    return [f"AOT_GPR_U64(31) = (u64)(s64)(s32)({_u32(pc)} + 8u);"]


def emit_block(block: BasicBlock) -> list[str]:
    lines: list[str] = []
    instructions = block.instructions
    term = block.terminator
    delay = block.delay_instruction

    if term is None:
        # A maximum-length block falls through.  All instructions are normal.
        for index, insn in enumerate(instructions):
            lines += emit_instruction(insn, index + 1, insn.vaddr + 4)
        lines.append(f"return AOTBlockResult{{{_u32(block.end_pc)}, {len(instructions)}u, AOT_STATUS_OK}};")
        return lines

    normal = [insn for insn in instructions if insn is not term and insn is not delay]
    for index, insn in enumerate(normal):
        # If a job is queued by a memory access in the middle, the next
        # instruction is the architectural PC.
        lines += emit_instruction(insn, index + 1, insn.vaddr + 4)

    executed_before_term = len(normal)
    kind = term.kind
    branch_target = term.branch_target
    fallthrough = term.vaddr + 8

    if kind in ("beq", "bne", "blez", "bgtz", "bltz", "bgez", "bltzal", "bgezal"):
        if branch_target is None:
            raise BuildError(f"internal error: branch at 0x{term.vaddr:08X} has no target")
        if kind in ("bltzal", "bgezal"):
            lines += _link_ra(term.vaddr)
        lines.append(f"const bool aot_taken = {_branch_condition(term)};")
        # If the delay slot queues a job, the branch has already been decided;
        # the architectural next PC is the taken or fall-through address.
        next_pc = f"(aot_taken ? {_u32(branch_target)} : {_u32(fallthrough)})"
        if delay is not None:
            lines += emit_instruction(delay, executed_before_term + 2, next_pc, delay_slot=True)
        lines.append(
            f"return AOTBlockResult{{{next_pc}, "
            f"{len(instructions)}u, AOT_STATUS_OK}};"
        )
        return lines

    if kind in ("j", "jal"):
        if branch_target is None:
            raise BuildError(f"internal error: jump at 0x{term.vaddr:08X} has no target")
        if kind == "jal":
            lines += _link_ra(term.vaddr)
        if delay is not None:
            lines += emit_instruction(delay, executed_before_term + 2, branch_target, delay_slot=True)
        lines.append(f"return AOTBlockResult{{{_u32(branch_target)}, {len(instructions)}u, AOT_STATUS_OK}};")
        return lines

    if kind in ("jr", "jalr"):
        target_reg = term.rs
        lines.append(f"const u32 aot_target = {_gpr_u32(target_reg)};")
        if kind == "jalr" and term.rd != 0:
            lines.append(f"AOT_GPR_U64({term.rd}) = (u64)(s64)(s32)({_u32(term.vaddr)} + 8u);")
        if delay is not None:
            lines += emit_instruction(delay, executed_before_term + 2, "aot_target", delay_slot=True)
        lines.append(f"return AOTBlockResult{{aot_target, {len(instructions)}u, AOT_STATUS_OK}};")
        return lines

    raise BuildError(f"internal error: unsupported terminator {kind} at 0x{term.vaddr:08X}")


def generate_program(
    blocks: list[BasicBlock],
    metadata: ProgramMetadata | None = None,
) -> GeneratedProgram:
    metadata = metadata or ProgramMetadata()
    generated: list[GeneratedBlock] = []
    seen: set[tuple[int, tuple[int, ...]]] = set()
    ordered = sorted(
        blocks,
        key=lambda item: (
            item.start,
            item.mapping_base,
            item.source_offset,
            tuple(item.words),
        ),
    )
    for block in ordered:
        if not block.aot_safe:
            continue
        words = tuple(block.words)
        key = (block.start, words)
        if key in seen:
            continue
        seen.add(key)
        body = emit_block(block)
        index = len(generated)
        name = f"n64to3ds_aot_{index:04d}_{block.start:08X}"
        source_lines = [
            f'extern "C" AOTBlockResult {name}()',
            "{",
            *[f"    {line}" for line in body],
            "}",
            "",
        ]
        generated.append(
            GeneratedBlock(
                start=block.start,
                name=name,
                instruction_count=len(words),
                fingerprint=fingerprint_words(words),
                source="\n".join(source_lines),
                words=words,
                mapping_base=block.mapping_base,
                source_offset=block.source_offset,
            )
        )

    # A compact human-readable summary of which mapping hypotheses contributed
    # blocks.  The runtime only needs the descriptor table; this string exists
    # so the on-device log can explain why some fingerprints may never match.
    hypothesis_stats: dict[int, tuple[int, int]] = {}
    for block in generated:
        count, instructions = hypothesis_stats.get(block.mapping_base, (0, 0))
        hypothesis_stats[block.mapping_base] = (
            count + 1,
            instructions + block.instruction_count,
        )
    summary_parts = [
        f"0x{base:08X}:{count} blocks/{instructions} insns"
        for base, (count, instructions) in sorted(hypothesis_stats.items())
    ]
    mapping_summary = "; ".join(summary_parts) if summary_parts else "none"

    header = [
        "// Generated by n64to3ds. Do not edit by hand.",
        "// This file is compiled into the DaedalusX64 3DS runtime.",
        '#include "AOTGeneratedCommon.h"',
        "",
        f'extern "C" const u32 g_n64to3ds_aot_mapping_base = {_u32(metadata.mapping_base)};',
        f'extern "C" const u32 g_n64to3ds_aot_mapped_size = {metadata.mapped_size}u;',
        f'extern "C" const char g_n64to3ds_aot_mapping_summary[] = "{_escape_c_string(mapping_summary)}";',
        f'extern "C" const char g_n64to3ds_aot_rom_sha256[] = "{metadata.rom_sha256}";',
        f'extern "C" const char g_n64to3ds_aot_game_name[] = "{_escape_c_string(metadata.game_name)}";',
        f'extern "C" const char g_n64to3ds_aot_mode[] = "{_escape_c_string(metadata.aot_mode)}";',
        "",
    ]
    body: list[str] = []
    for block in generated:
        body.append(block.source)

    state_count = max(1, len(generated))
    table_lines: list[str] = [
        f"static AOTBlockRuntimeState kAOTBlockState[{state_count}];",
        'extern "C" AOTBlockRuntimeState *const g_n64to3ds_aot_state = kAOTBlockState;',
    ]
    if generated:
        table_lines.append("static const AOTBlockDescriptor kAOTBlocks[] = {")
        for block in generated:
            table_lines.append(
                f"    {{{_u32(block.start)}, {_u32(block.source_offset)}, "
                f"{block.instruction_count}u, 0u, "
                f"0x{block.fingerprint:016X}ull, {block.name}}},"
            )
        table_lines.append("};")
        table_lines.append(
            'extern "C" const AOTBlockDescriptor *const g_n64to3ds_aot_blocks = kAOTBlocks;'
        )
        table_lines.append(f'extern "C" const u32 g_n64to3ds_aot_block_count = {len(generated)}u;')
    else:
        table_lines.append(
            'extern "C" const AOTBlockDescriptor *const g_n64to3ds_aot_blocks = nullptr;'
        )
        table_lines.append('extern "C" const u32 g_n64to3ds_aot_block_count = 0u;')
    table_lines.append("")

    source = "\n".join(header + body + table_lines)
    return GeneratedProgram(blocks=generated, source=source, metadata=metadata)


def write_generated(program: GeneratedProgram, destination: str | Path) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(program.source, encoding="utf-8")
    return path


def write_manifest(program: GeneratedProgram, destination: str | Path) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(program.as_dict(), indent=2) + "\n", encoding="utf-8")
    return path

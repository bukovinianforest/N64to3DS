"""Conservative static MIPS analysis for n64to3ds.

The analyzer never performs a linear sweep over a ROM and calls the result
"code".  It starts from architectural entry points, follows direct control flow
and only emits blocks whose bytes are valid MIPS instructions in a known
ROM-to-RDRAM mapping.  Anything else is left to Daedalus' Dynarec/interpreter.

Rabbitizer is used as the instruction decoder/validator.  The small opcode
classification table below exists because we need stable *canonical* operation
names for code generation; Rabbitizer's display names intentionally include
pseudo-instructions (bnez, move, neg, ...).
"""

from __future__ import annotations

import collections
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import AnalyzerError
from .rom import NormalizedRom

try:
    from rabbitizer import Instruction as RabbitInstruction
except Exception:  # pragma: no cover - dependency check happens in CLI
    RabbitInstruction = None


ROM_CODE_START = 0x1000
STANDARD_LOAD_ADDRESS = 0x80000400
MAX_IPL3_LOAD = 0x100000  # IPL3 loads the first 1 MiB to 0x80000400.
DEFAULT_MAX_BLOCKS = 4096
DEFAULT_MAX_INSTRUCTIONS = 16384
DEFAULT_MAX_BLOCK_INSTRUCTIONS = 64


# Canonical N64/MIPS III operation names used by the generator.
_SPECIAL = {
    0x00: "sll",
    0x02: "srl",
    0x03: "sra",
    0x04: "sllv",
    0x06: "srlv",
    0x07: "srav",
    0x08: "jr",
    0x09: "jalr",
    0x0C: "syscall",
    0x0D: "break",
    0x0F: "sync",
    0x10: "mfhi",
    0x11: "mthi",
    0x12: "mflo",
    0x13: "mtlo",
    0x18: "mult",
    0x19: "multu",
    0x1A: "div",
    0x1B: "divu",
    0x1C: "dmult",
    0x1D: "dmultu",
    0x1E: "ddiv",
    0x1F: "ddivu",
    0x20: "add",
    0x21: "addu",
    0x22: "sub",
    0x23: "subu",
    0x24: "and",
    0x25: "or",
    0x26: "xor",
    0x27: "nor",
    0x2A: "slt",
    0x2B: "sltu",
    0x2C: "dadd",
    0x2D: "daddu",
    0x2E: "dsub",
    0x2F: "dsubu",
    0x30: "tge",
    0x31: "tgeu",
    0x32: "tlt",
    0x33: "tltu",
    0x34: "teq",
    0x36: "tne",
    0x38: "dsll",
    0x3A: "dsrl",
    0x3B: "dsra",
    0x3C: "dsll32",
    0x3E: "dsrl32",
    0x3F: "dsra32",
}

_BRANCHES = {0x04: "beq", 0x05: "bne", 0x06: "blez", 0x07: "bgtz"}
_BRANCH_LIKELY = {0x14: "beql", 0x15: "bnel", 0x16: "blezl", 0x17: "bgtzl"}
_REGIMM = {
    0x00: "bltz",
    0x01: "bgez",
    0x02: "bltzl",
    0x03: "bgezl",
    0x08: "tgei",
    0x09: "tgeiu",
    0x0A: "tlti",
    0x0B: "tltiu",
    0x0C: "teqi",
    0x0E: "tnei",
    0x10: "bltzal",
    0x11: "bgezal",
    0x12: "bltzall",
    0x13: "bgezall",
}
_LOADS = {
    0x20: "lb",
    0x21: "lh",
    0x22: "lwl",
    0x23: "lw",
    0x24: "lbu",
    0x25: "lhu",
    0x26: "lwr",
    0x27: "lwu",
    0x30: "ll",
    0x31: "lwc1",
    0x32: "lwc2",
    0x33: "lld",
    0x35: "ldc1",
    0x37: "ld",
}
_STORES = {
    0x28: "sb",
    0x29: "sh",
    0x2A: "swl",
    0x2B: "sw",
    0x2C: "sdl",
    0x2D: "sdr",
    0x2E: "swr",
    0x38: "sc",
    0x39: "swc1",
    0x3A: "swc2",
    0x3B: "scd",
    0x3D: "sdc1",
    0x3F: "sd",
}
_IMMEDIATE = {
    0x08: "addi",
    0x09: "addiu",
    0x0A: "slti",
    0x0B: "sltiu",
    0x0C: "andi",
    0x0D: "ori",
    0x0E: "xori",
    0x0F: "lui",
    0x18: "daddi",
    0x19: "daddiu",
}

_SUPPORTED = {
    # ALU/immediate
    "addi", "addiu", "daddi", "daddiu", "slti", "sltiu", "andi", "ori", "xori", "lui",
    # word/doubleword ALU
    "sll", "srl", "sra", "sllv", "srlv", "srav",
    "add", "addu", "sub", "subu", "and", "or", "xor", "nor",
    "slt", "sltu", "dadd", "daddu", "dsub", "dsubu",
    "dsll", "dsrl", "dsra", "dsll32", "dsrl32", "dsra32",
    # HI/LO (32-bit multiply/divide only; Daedalus' DMULT/DDIV semantics
    # depend on build options, so leave those to the interpreter for now)
    "mfhi", "mthi", "mflo", "mtlo", "mult", "multu", "div", "divu",
    # branches/jumps
    "beq", "bne", "blez", "bgtz", "bltz", "bgez", "bltzal", "bgezal",
    "j", "jal", "jr", "jalr",
    # integer memory
    "lb", "lbu", "lh", "lhu", "lw", "lwu", "ld",
    "sb", "sh", "sw", "sd",
    # harmless
    "sync", "nop",
}


@dataclass(frozen=True)
class InstructionInfo:
    vaddr: int
    word: int
    opcode: int
    rs: int
    rt: int
    rd: int
    sa: int
    funct: int
    imm: int
    target26: int
    kind: str
    text: str
    valid: bool
    supported: bool
    unsupported_reason: str | None
    is_branch: bool
    is_jump: bool
    is_likely: bool
    has_delay: bool
    branch_target: int | None

    @property
    def is_terminator(self) -> bool:
        return self.is_branch or self.is_jump

    @property
    def is_conditional(self) -> bool:
        return self.is_branch and not self.is_jump


@dataclass(frozen=True)
class RomMapping:
    base_vaddr: int
    rom_start: int
    rom_end: int
    score: int
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return self.rom_end - self.rom_start

    def contains(self, vaddr: int) -> bool:
        return self.base_vaddr <= vaddr < self.base_vaddr + self.size

    def to_offset(self, vaddr: int) -> int | None:
        if not self.contains(vaddr):
            return None
        return self.rom_start + (vaddr - self.base_vaddr)


@dataclass
class BasicBlock:
    start: int
    instructions: list[InstructionInfo]
    delay_included: bool
    end_reason: str
    terminator: InstructionInfo | None
    delay_instruction: InstructionInfo | None
    references: int = 0
    # The hypothesis this block was proven under.  A ROM can have more than
    # one plausible ROM-offset -> virtual-address mapping (for example a custom
    # IPL3/boot loader that uses the header entry as its load address), so the
    # runtime must be able to tell otherwise-identical virtual addresses apart.
    mapping_base: int = 0
    source_offset: int = 0

    @property
    def end_pc(self) -> int:
        return self.instructions[-1].vaddr + 4

    @property
    def words(self) -> list[int]:
        return [insn.word for insn in self.instructions]

    @property
    def instruction_count(self) -> int:
        return len(self.instructions)

    @property
    def aot_safe(self) -> bool:
        if not self.instructions:
            return False
        if any(not insn.supported for insn in self.instructions):
            return False
        if self.terminator is not None:
            if not self.terminator.supported:
                return False
            # A branch/jump without its delay slot would change program order.
            if self.terminator.has_delay and not self.delay_included:
                return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": f"0x{self.start:08X}",
            "end": f"0x{self.end_pc:08X}",
            "instructions": self.instruction_count,
            "delay_included": self.delay_included,
            "end_reason": self.end_reason,
            "terminator": self.terminator.kind if self.terminator else None,
            "references": self.references,
            "mapping_base": f"0x{self.mapping_base:08X}",
            "source_offset": f"0x{self.source_offset:08X}",
            "words": [f"0x{word:08X}" for word in self.words],
        }


@dataclass
class AnalysisResult:
    mapping: RomMapping
    candidates: list[dict[str, Any]]
    blocks: list[BasicBlock]
    selected: list[BasicBlock]
    stats: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mapping": {
                "base_vaddr": f"0x{self.mapping.base_vaddr:08X}",
                "rom_start": f"0x{self.mapping.rom_start:08X}",
                "rom_end": f"0x{self.mapping.rom_end:08X}",
                "size": self.mapping.size,
                "score": self.mapping.score,
                "details": self.mapping.details,
            },
            "candidates": self.candidates,
            "hypotheses": list(self.hypotheses),
            "stats": self.stats,
            "warnings": list(self.warnings),
            "blocks": [block.as_dict() for block in self.blocks],
            "selected": [block.as_dict() for block in self.selected],
        }


def _sign_extend(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value & (sign - 1)) - (value & sign)


def decode_instruction(vaddr: int, word: int) -> InstructionInfo:
    if RabbitInstruction is None:
        raise AnalyzerError(
            "rabbitizer is not installed; the MIPS analyzer cannot run.",
            hint="Install pinned dependencies with: python3 -m pip install -r requirements.txt",
        )

    rabbit = RabbitInstruction(word)
    valid = bool(rabbit.isValid())
    text = rabbit.disassemble() if valid else f".word 0x{word:08X}"
    op = (word >> 26) & 0x3F
    rs = (word >> 21) & 0x1F
    rt = (word >> 16) & 0x1F
    rd = (word >> 11) & 0x1F
    sa = (word >> 6) & 0x1F
    funct = word & 0x3F
    imm = word & 0xFFFF
    target26 = word & 0x03FFFFFF

    kind = "invalid"
    supported = False
    reason: str | None = None
    is_branch = False
    is_jump = False
    is_likely = False
    has_delay = False
    branch_target: int | None = None

    if not valid:
        reason = "invalid instruction encoding"
    elif op == 0x00:
        kind = _SPECIAL.get(funct, "special?")
        if kind == "special?":
            reason = f"unsupported SPECIAL funct 0x{funct:02X}"
        else:
            supported = kind in _SUPPORTED
            if not supported:
                reason = f"unsupported special op {kind}"
        is_jump = kind in ("jr", "jalr")
        has_delay = is_jump
        is_branch = is_jump
        if kind == "nop":
            supported = True
            reason = None
    elif op == 0x01:
        kind = _REGIMM.get(rt, "regimm?")
        if kind == "regimm?":
            reason = f"unsupported REGIMM rt 0x{rt:02X}"
        else:
            supported = kind in _SUPPORTED
            if not supported:
                reason = f"unsupported regimm op {kind}"
            if kind.startswith(("bltz", "bgez")):
                is_branch = True
                has_delay = True
                is_likely = kind.endswith("l") or kind.endswith("ll")
                branch_target = (vaddr + _sign_extend(imm, 16) * 4 + 4) & 0xFFFFFFFF
    elif op in (0x02, 0x03):
        kind = "j" if op == 0x02 else "jal"
        supported = True
        is_branch = True
        is_jump = True
        has_delay = True
        branch_target = (vaddr & 0xF0000000) | (target26 << 2)
    elif op in _BRANCHES or op in _BRANCH_LIKELY:
        kind = _BRANCHES.get(op) or _BRANCH_LIKELY[op]
        is_likely = op in _BRANCH_LIKELY
        is_branch = True
        has_delay = True
        branch_target = (vaddr + _sign_extend(imm, 16) * 4 + 4) & 0xFFFFFFFF
        supported = kind in _SUPPORTED and not is_likely
        if is_likely:
            reason = "branch-likely instruction; left to interpreter"
    elif op in _IMMEDIATE:
        kind = _IMMEDIATE[op]
        supported = kind in _SUPPORTED
    elif op in _LOADS:
        kind = _LOADS[op]
        supported = kind in _SUPPORTED
        if not supported:
            reason = f"unsupported load {kind}"
    elif op in _STORES:
        kind = _STORES[op]
        supported = kind in _SUPPORTED
        if not supported:
            reason = f"unsupported store {kind}"
    elif op == 0x10:
        kind = "cop0"
        reason = "CP0/TLB instruction; left to interpreter"
    elif op == 0x11:
        kind = "cop1"
        reason = "FPU instruction; left to interpreter"
    elif op == 0x12:
        kind = "cop2"
        reason = "COP2 instruction; left to interpreter"
    else:
        kind = f"op{op:02X}"
        reason = f"unsupported opcode 0x{op:02X}"

    if kind == "nop":
        supported = True
    if not valid:
        supported = False
    if supported and reason:
        supported = False

    return InstructionInfo(
        vaddr=vaddr,
        word=word,
        opcode=op,
        rs=rs,
        rt=rt,
        rd=rd,
        sa=sa,
        funct=funct,
        imm=imm,
        target26=target26,
        kind=kind,
        text=text,
        valid=valid,
        supported=supported,
        unsupported_reason=reason,
        is_branch=is_branch,
        is_jump=is_jump,
        is_likely=is_likely,
        has_delay=has_delay,
        branch_target=branch_target,
    )


class _InstructionCache:
    def __init__(self, rom: NormalizedRom, mapping: RomMapping):
        self._rom = rom
        self._mapping = mapping
        self._cache: dict[int, InstructionInfo] = {}

    def get(self, vaddr: int) -> InstructionInfo | None:
        if vaddr in self._cache:
            return self._cache[vaddr]
        off = self._mapping.to_offset(vaddr)
        if off is None or off + 4 > len(self._rom.data) or vaddr % 4:
            return None
        word = int.from_bytes(self._rom.data[off:off + 4], "big")
        info = decode_instruction(vaddr, word)
        self._cache[vaddr] = info
        return info


def _score_sequence(cache: _InstructionCache, start: int, limit: int = 64) -> tuple[int, dict[str, Any]]:
    score = 0
    valid = 0
    supported = 0
    branches = 0
    prologue = 0
    pc = start
    kinds: list[str] = []
    for index in range(limit):
        info = cache.get(pc)
        if info is None or not info.valid:
            break
        valid += 1
        kinds.append(info.kind)
        if info.supported:
            supported += 2
        else:
            score += 0
        if info.is_branch or info.is_jump:
            branches += 1
        if info.branch_target is not None and cache.get(info.branch_target) is not None:
            score += 2
        if index < 6:
            if info.kind == "addiu" and info.rs == 29 and _sign_extend(info.imm, 16) < 0:
                prologue += 12
            elif info.kind == "sw" and info.rt == 31 and info.rs == 29:
                prologue += 12
            elif info.kind == "lui" and index < 2:
                prologue += 3
        pc += 4
    score += valid + supported + branches * 2 + prologue
    if valid < 8:
        score -= 40
    details = {
        "valid": valid,
        "supported": supported,
        "branches": branches,
        "prologue_bonus": prologue,
        "first_kinds": kinds[:12],
    }
    return score, details


def _score_mapping_candidates(rom: NormalizedRom) -> tuple[list[RomMapping], list[dict[str, Any]], list[str]]:
    """Score plausible ROM-offset -> virtual-address mappings.

    The standard IPL3 load address is 0x80000400, but some ROMs contain a
    custom boot loader that uses the header entry point as the load address
    (Harvest Moon 64 is one example).  We therefore score the standard
    address, the header entry point and 0x80000000, then let the runtime
    fingerprint the generated blocks.  A score is only a search heuristic;
    it is never treated as proof that a mapping is correct.
    """

    warnings: list[str] = []
    available = min(len(rom.data), ROM_CODE_START + MAX_IPL3_LOAD)
    rom_end = available
    if rom_end <= ROM_CODE_START:
        raise AnalyzerError("ROM has no code area after the 4 KiB boot header.")
    if len(rom.data) > available:
        warnings.append(
            f"Only the first {MAX_IPL3_LOAD // 1024} KiB of the ROM (offsets "
            f"0x{ROM_CODE_START:X}-0x{available:X}) are statically mapped. "
            "Overlays and code DMA'd elsewhere are intentionally left to Dynarec."
        )

    candidates_bases = [STANDARD_LOAD_ADDRESS, 0x80000000, rom.header.entry_point]

    seen: set[int] = set()
    candidates: list[dict[str, Any]] = []
    mappings: list[RomMapping] = []

    for base in candidates_bases:
        if base in seen:
            continue
        seen.add(base)
        mapping = RomMapping(base_vaddr=base, rom_start=ROM_CODE_START, rom_end=rom_end, score=0)
        cache = _InstructionCache(rom, mapping)
        total = 0
        details: dict[str, Any] = {}
        base_score, base_details = _score_sequence(cache, base)
        total += base_score
        details["base"] = base_details
        entry = rom.header.entry_point
        if mapping.contains(entry):
            entry_score, entry_details = _score_sequence(cache, entry)
            total += entry_score
            details["entry"] = entry_details
        else:
            total -= 30
            details["entry"] = {"mapped": False}
        # Follow a small number of direct branch targets from the entry points
        # to reward mappings whose control flow lands on real code.
        targets: set[int] = set()
        for seed in (base, entry):
            if not mapping.contains(seed):
                continue
            pc = seed
            for _ in range(32):
                info = cache.get(pc)
                if info is None or not info.valid:
                    break
                if info.branch_target is not None:
                    targets.add(info.branch_target)
                if info.is_terminator:
                    break
                pc += 4
        target_scores = []
        for target in list(targets)[:16]:
            if mapping.contains(target):
                score, target_details = _score_sequence(cache, target, limit=24)
                total += max(0, score // 2)
                target_scores.append({"target": f"0x{target:08X}", "score": score, "details": target_details})
        details["targets"] = target_scores
        candidate = RomMapping(base_vaddr=base, rom_start=ROM_CODE_START, rom_end=rom_end, score=total, details=details)
        mappings.append(candidate)
        candidates.append(
            {
                "base_vaddr": f"0x{base:08X}",
                "score": total,
                "details": details,
            }
        )

    mappings.sort(key=lambda item: item.score, reverse=True)
    candidates.sort(key=lambda item: item["score"], reverse=True)
    if mappings[0].score < 0:
        warnings.append(
            "No high-confidence ROM-to-RDRAM mapping was found; AOT will be disabled "
            "unless blocks can still be proven from an entry point."
        )
    return mappings, candidates, warnings


def choose_mapping(rom: NormalizedRom) -> tuple[RomMapping, list[dict[str, Any]], list[str]]:
    """Return the best-scoring mapping plus all candidate scores."""

    mappings, candidates, warnings = _score_mapping_candidates(rom)
    return mappings[0], candidates, warnings


def _add_leader(leaders: collections.deque[int], queued: set[int], address: int, mapping: RomMapping) -> None:
    if address % 4 or not mapping.contains(address):
        return
    if address not in queued:
        queued.add(address)
        leaders.append(address)


def _known_jump_target(block: BasicBlock) -> int | None:
    """Resolve a ``jr``/``jalr`` target when it is a local constant.

    The N64 boot stub ends with::

        lui   $t2, 0x8002
        addiu $t2, $t2, 0x5c50
        jr    $t2

    The target is not visible to a naive CFG walk because the register is
    loaded at runtime.  This tiny forward constant tracker resolves only the
    values built inside the same basic block (LUI/ORI/ADDIU/ADDU).  It never
    guesses a value loaded from memory, so it cannot turn data into code.
    """

    term = block.terminator
    if term is None or term.kind not in ("jr", "jalr"):
        return None

    constants: dict[int, int] = {}
    for insn in block.instructions:
        if insn is term:
            break
        rt = insn.rt
        rs = insn.rs
        if insn.kind == "lui":
            constants[rt] = (insn.imm << 16) & 0xFFFFFFFF
        elif insn.kind == "ori" and rs in constants:
            constants[rt] = (constants[rs] | insn.imm) & 0xFFFFFFFF
        elif insn.kind in ("addi", "addiu") and rs in constants:
            constants[rt] = (constants[rs] + _sign_extend(insn.imm, 16)) & 0xFFFFFFFF
        elif insn.kind in ("addu", "daddu") and rs in constants and insn.rt in constants:
            constants[insn.rd] = (constants[rs] + constants[insn.rt]) & 0xFFFFFFFF
        else:
            # Any other write destroys our knowledge.  Reads do not.
            if insn.kind in _WRITES_RT:
                constants.pop(rt, None)
            if insn.kind in _WRITES_RD:
                constants.pop(insn.rd, None)

    target_register = term.rs
    if term.kind == "jalr" and term.rd != 0:
        target_register = term.rs
    if target_register in constants:
        return constants[target_register] & 0xFFFFFFFF
    return None


_WRITES_RT = {
    "addi", "addiu", "daddi", "daddiu", "slti", "sltiu", "andi", "ori", "xori", "lui",
    "lb", "lbu", "lh", "lhu", "lw", "lwu", "ld", "lwl", "lwr", "ll", "lld",
    "mfhi", "mflo", "mult", "multu", "div", "divu", "dmult", "dmultu", "ddiv", "ddivu",
}
_WRITES_RD = {
    "sll", "srl", "sra", "sllv", "srlv", "srav",
    "add", "addu", "sub", "subu", "and", "or", "xor", "nor", "slt", "sltu",
    "dadd", "daddu", "dsub", "dsubu", "dsll", "dsrl", "dsra", "dsll32", "dsrl32", "dsra32",
    "addu", "daddu", "jalr",
}


def _analyze_with_mapping(
    rom: NormalizedRom,
    mapping: RomMapping,
    *,
    max_blocks: int = DEFAULT_MAX_BLOCKS,
    max_instructions: int = DEFAULT_MAX_INSTRUCTIONS,
    max_block_instructions: int = DEFAULT_MAX_BLOCK_INSTRUCTIONS,
    warnings: Iterable[str] = (),
) -> AnalysisResult:
    """Discover and select blocks for one explicit mapping hypothesis."""

    warnings = list(warnings)
    cache = _InstructionCache(rom, mapping)
    leaders: collections.deque[int] = collections.deque()
    queued: set[int] = set()
    _add_leader(leaders, queued, mapping.base_vaddr, mapping)
    _add_leader(leaders, queued, rom.header.entry_point, mapping)

    blocks_by_start: dict[int, BasicBlock] = {}
    delay_slots: set[int] = set()
    unsupported_counts: collections.Counter[str] = collections.Counter()
    unsupported_examples: dict[str, str] = {}
    discovered_targets: set[int] = set()
    processed: set[int] = set()

    while leaders:
        start = leaders.popleft()
        if start in processed:
            continue
        processed.add(start)
        if start in delay_slots:
            # A branch target inside a delay slot is architecturally
            # questionable; do not create a block with the wrong Delay state.
            continue

        instructions: list[InstructionInfo] = []
        terminator: InstructionInfo | None = None
        delay_instruction: InstructionInfo | None = None
        delay_included = False
        end_reason = "fallthrough"
        pc = start

        while len(instructions) < max_block_instructions:
            if pc in delay_slots and pc != start:
                end_reason = "entered_delay_slot"
                break
            info = cache.get(pc)
            if info is None:
                end_reason = "outside_mapping_or_unaligned"
                break
            if not info.valid:
                end_reason = "invalid"
                unsupported_counts[info.kind] += 1
                unsupported_examples.setdefault(info.kind, f"0x{pc:08X}: {info.text}")
                break
            if not info.supported:
                end_reason = f"unsupported:{info.kind}"
                unsupported_counts[info.kind] += 1
                unsupported_examples.setdefault(info.kind, f"0x{pc:08X}: {info.text}")
                # Even when we cannot AOT-compile a branch, follow its direct
                # targets so later supported blocks are not lost.
                if info.is_terminator:
                    _queue_after_terminator(info, pc, mapping, leaders, queued, delay_slots, discovered_targets)
                else:
                    # The interpreter will execute this instruction and may
                    # continue at pc+4.  That address is still a proven code
                    # entry even though this particular block cannot be AOT.
                    _add_leader(leaders, queued, pc + 4, mapping)
                break

            instructions.append(info)
            if info.is_terminator:
                terminator = info
                delay_pc = pc + 4
                if info.has_delay and mapping.contains(delay_pc):
                    delay_slots.add(delay_pc)
                    dinfo = cache.get(delay_pc)
                    if dinfo is not None and dinfo.valid and dinfo.supported and not dinfo.is_terminator:
                        instructions.append(dinfo)
                        delay_instruction = dinfo
                        delay_included = True
                        end_reason = "terminator_with_delay"
                    else:
                        end_reason = "terminator_delay_left_to_interpreter"
                        if dinfo is not None and not dinfo.supported:
                            unsupported_counts[dinfo.kind] += 1
                            unsupported_examples.setdefault(dinfo.kind, f"0x{delay_pc:08X}: {dinfo.text}")
                else:
                    end_reason = "terminator_without_mapped_delay"
                _queue_after_terminator(info, pc, mapping, leaders, queued, delay_slots, discovered_targets)
                break
            pc += 4

        if not instructions:
            continue
        if terminator is None and len(instructions) >= max_block_instructions:
            end_reason = "max_block_length"
            _add_leader(leaders, queued, pc, mapping)

        block = BasicBlock(
            start=start,
            instructions=instructions,
            delay_included=delay_included,
            end_reason=end_reason,
            terminator=terminator,
            delay_instruction=delay_instruction,
            mapping_base=mapping.base_vaddr,
            source_offset=mapping.to_offset(start) or 0,
        )
        blocks_by_start[start] = block

        # Resolve jr/jalr only when the target register was built from
        # constants inside this same block.  This is how the standard IPL3
        # boot stub reaches the game entry point.
        indirect_target = _known_jump_target(block)
        if indirect_target is not None:
            discovered_targets.add(indirect_target)
            _add_leader(leaders, queued, indirect_target, mapping)

    # Count incoming references for selection.
    for block in blocks_by_start.values():
        term = block.terminator
        if term is None:
            continue
        if term.branch_target is not None:
            target = term.branch_target
            if target in blocks_by_start:
                blocks_by_start[target].references += 1
        if term.is_conditional:
            fallthrough = block.end_pc
            if fallthrough in blocks_by_start:
                blocks_by_start[fallthrough].references += 1

    all_blocks = sorted(blocks_by_start.values(), key=lambda b: b.start)
    safe_blocks = [block for block in all_blocks if block.aot_safe]

    # Select by a deterministic score.  Entry blocks and blocks with many
    # references are most likely to be executed early and often.
    def selection_score(block: BasicBlock) -> tuple[int, int, int]:
        score = block.references * 100
        score += block.instruction_count * 3
        if block.start in (mapping.base_vaddr, rom.header.entry_point):
            score += 100000
        if block.terminator and block.terminator.branch_target is not None:
            if block.terminator.branch_target <= block.start:
                score += 250
        return (score, -block.start, block.instruction_count)

    selected: list[BasicBlock] = []
    selected_instructions = 0
    for block in sorted(safe_blocks, key=selection_score, reverse=True):
        if len(selected) >= max_blocks:
            break
        if selected_instructions + block.instruction_count > max_instructions:
            continue
        selected.append(block)
        selected_instructions += block.instruction_count
    selected.sort(key=lambda b: (b.start, b.source_offset))

    stats = {
        "mapping_base": f"0x{mapping.base_vaddr:08X}",
        "mapped_bytes": mapping.size,
        "candidate_blocks": len(all_blocks),
        "safe_blocks": len(safe_blocks),
        "selected_blocks": len(selected),
        "selected_instructions": selected_instructions,
        "selected_words": sum(block.instruction_count for block in selected),
        "max_blocks": max_blocks,
        "max_instructions": max_instructions,
        "unsupported_counts": dict(sorted(unsupported_counts.items())),
        "unsupported_examples": unsupported_examples,
        "entry_points": [f"0x{mapping.base_vaddr:08X}", f"0x{rom.header.entry_point:08X}"],
        "discovered_targets": len(discovered_targets),
        "truncated": len(selected) < len(safe_blocks),
    }
    if not selected:
        warnings.append("No safe AOT blocks were found; the build will use Daedalus' interpreter/Dynarec fallback.")
    if stats["truncated"]:
        warnings.append(
            f"AOT selection was capped at {max_blocks} blocks / {max_instructions} instructions; "
            "the remaining safe blocks stay on the Dynarec fallback."
        )
    return AnalysisResult(
        mapping=mapping,
        candidates=[],
        blocks=all_blocks,
        selected=selected,
        stats=stats,
        warnings=warnings,
    )


def analyze(
    rom: NormalizedRom,
    *,
    max_blocks: int = DEFAULT_MAX_BLOCKS,
    max_instructions: int = DEFAULT_MAX_INSTRUCTIONS,
    max_block_instructions: int = DEFAULT_MAX_BLOCK_INSTRUCTIONS,
) -> AnalysisResult:
    """Analyze the best-scoring mapping hypothesis."""

    mapping, candidates, warnings = choose_mapping(rom)
    result = _analyze_with_mapping(
        rom,
        mapping,
        max_blocks=max_blocks,
        max_instructions=max_instructions,
        max_block_instructions=max_block_instructions,
        warnings=warnings,
    )
    result.candidates = candidates
    return result


def _hypothesis_mappings(rom: NormalizedRom, max_hypotheses: int) -> tuple[list[RomMapping], list[dict[str, Any]], list[str]]:
    """Pick the mappings worth compiling.

    The runtime fingerprints every block against resident RDRAM, so a wrong
    hypothesis is harmless: its blocks simply never validate.  We include the
    best-scoring candidate plus the standard IPL3 address and the header entry
    point when they differ, because those two account for the common normal
    and custom-boot-loader layouts.  Additional candidates only fill unused
    slots; they never displace the two structural hypotheses.
    """

    mappings, candidates, warnings = _score_mapping_candidates(rom)
    if not mappings or max_hypotheses <= 0:
        return [], candidates, warnings

    by_base = {mapping.base_vaddr: mapping for mapping in mappings}
    ordered: list[RomMapping] = []

    def add(mapping: RomMapping | None) -> None:
        if mapping is not None and all(existing.base_vaddr != mapping.base_vaddr for existing in ordered):
            ordered.append(mapping)

    # The best data-driven candidate gets the first slot.
    add(mappings[0])
    # Structural hypotheses.  These are not claims that the mapping is
    # correct; they are the two layouts this tool knows how to prove statically.
    add(by_base.get(STANDARD_LOAD_ADDRESS))
    add(by_base.get(rom.header.entry_point))
    # Fill any remaining slots with the next-best scored candidates.
    for mapping in mappings:
        if len(ordered) >= max_hypotheses:
            break
        add(mapping)
    ordered = ordered[:max_hypotheses]
    ordered.sort(key=lambda item: item.score, reverse=True)
    return ordered, candidates, warnings


def analyze_multi(
    rom: NormalizedRom,
    *,
    max_hypotheses: int = 3,
    max_blocks: int = DEFAULT_MAX_BLOCKS,
    max_instructions: int = DEFAULT_MAX_INSTRUCTIONS,
    max_block_instructions: int = DEFAULT_MAX_BLOCK_INSTRUCTIONS,
) -> AnalysisResult:
    """Analyze several mapping hypotheses and merge their proven blocks.

    Each hypothesis gets a share of the global block/instruction budget.  A
    block is identified by ``(start, words)``; the same virtual address may
    legitimately appear more than once when two hypotheses map it to different
    ROM bytes.  The generated descriptor carries the source offset so the host
    semantic test can load the exact bytes the block was proven from, and the
    runtime tries every descriptor at a PC until one fingerprint matches.
    """

    mappings, candidates, warnings = _hypothesis_mappings(rom, max_hypotheses)
    if not mappings:
        raise AnalyzerError("No ROM-to-RDRAM mapping candidates were found.")

    per_blocks = max(1, max_blocks // len(mappings))
    per_instructions = max(1, max_instructions // len(mappings))
    results: list[AnalysisResult] = []
    for mapping in mappings:
        result = _analyze_with_mapping(
            rom,
            mapping,
            max_blocks=per_blocks,
            max_instructions=per_instructions,
            max_block_instructions=max_block_instructions,
        )
        results.append(result)

    merged: dict[tuple[int, tuple[int, ...]], BasicBlock] = {}
    for result in results:
        for block in result.selected:
            key = (block.start, tuple(block.words))
            if key not in merged:
                merged[key] = block

    selected = sorted(
        merged.values(),
        key=lambda block: (block.start, block.mapping_base, block.source_offset, block.instruction_count),
    )
    selected_instructions = sum(block.instruction_count for block in selected)

    primary = results[0]
    hypotheses: list[dict[str, Any]] = []
    for mapping, result in zip(mappings, results):
        hypotheses.append(
            {
                "base_vaddr": f"0x{mapping.base_vaddr:08X}",
                "score": mapping.score,
                "mapped_size": mapping.size,
                "candidate_blocks": result.stats["candidate_blocks"],
                "safe_blocks": result.stats["safe_blocks"],
                "selected_blocks": result.stats["selected_blocks"],
                "selected_instructions": result.stats["selected_instructions"],
                "unsupported_counts": result.stats["unsupported_counts"],
            }
        )
        warnings.extend(
            f"mapping 0x{mapping.base_vaddr:08X}: {warning}" for warning in result.warnings
        )

    # Report the union of discovered blocks for the primary hypothesis only;
    # the selected list below contains the merged blocks from every hypothesis.
    stats = dict(primary.stats)
    stats.update(
        {
            "hypothesis_count": len(mappings),
            "selected_blocks": len(selected),
            "selected_instructions": selected_instructions,
            "selected_words": selected_instructions,
            "per_hypothesis": hypotheses,
            "max_blocks": max_blocks,
            "max_instructions": max_instructions,
            "per_hypothesis_blocks": per_blocks,
            "per_hypothesis_instructions": per_instructions,
        }
    )
    if not selected:
        warnings.append(
            "No safe AOT blocks were found in any mapping hypothesis; the build "
            "will use Daedalus' interpreter/Dynarec fallback."
        )
    if len(selected) > max_blocks:
        warnings.append(
            f"Merged AOT selection exceeded {max_blocks} blocks; truncating deterministically."
        )
        selected = selected[:max_blocks]
        selected_instructions = sum(block.instruction_count for block in selected)
        stats["selected_blocks"] = len(selected)
        stats["selected_instructions"] = selected_instructions
        stats["selected_words"] = selected_instructions

    return AnalysisResult(
        mapping=primary.mapping,
        candidates=candidates,
        blocks=primary.blocks,
        selected=selected,
        stats=stats,
        warnings=warnings,
        hypotheses=hypotheses,
    )


def _queue_after_terminator(
    info: InstructionInfo,
    pc: int,
    mapping: RomMapping,
    leaders: collections.deque[int],
    queued: set[int],
    delay_slots: set[int],
    discovered_targets: set[int],
) -> None:
    if info.has_delay:
        delay_slots.add(pc + 4)
    if info.branch_target is not None:
        discovered_targets.add(info.branch_target)
        _add_leader(leaders, queued, info.branch_target, mapping)
    # A call returns to the instruction after its delay slot.  Queueing that
    # address is conservative: it is a real instruction boundary regardless of
    # whether the callee actually returns, and it lets the CFG continue through
    # leaf functions instead of stopping at every `jr $ra`.
    if info.is_conditional or info.kind in ("jal", "jalr"):
        fallthrough = pc + 8 if info.has_delay else pc + 4
        _add_leader(leaders, queued, fallthrough, mapping)


def write_report(result: AnalysisResult, destination: str | Path) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.as_dict(), indent=2) + "\n", encoding="utf-8")
    return path

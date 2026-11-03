#!/usr/bin/env python3
"""Summarise a dynarec code dump (sdmc:/3ds/n64to3ds-agent/codedump.bin).

usage: scripts/codedump_report.py codedump.bin [--show N] [--fragment PC]

Static ARM instruction counts per MIPS instruction, weighted by how often
each fragment ran (its main path; slow paths live in another buffer).
"""

from __future__ import annotations

import argparse
import collections
import re
import struct
import subprocess
import tempfile
from pathlib import Path

import rabbitizer

ROOT = Path(__file__).resolve().parents[1]
OBJDUMP = ROOT / "third_party/devkitpro-root/opt/devkitpro/devkitARM/bin/arm-none-eabi-objdump"


def parse(path: Path):
    data = path.read_bytes()
    assert data[:8] == b"N64FRAG1", data[:8]
    (count,) = struct.unpack_from("<I", data, 8)
    pos = 12
    frags = []
    for _ in range(count):
        pc, hits, size, nops, addr = struct.unpack_from("<5I", data, pos)
        pos += 20
        ops = struct.unpack_from(f"<{nops * 2}I", data, pos)
        pos += nops * 8
        code = data[pos : pos + size]
        pos += size
        frags.append(dict(pc=pc, hits=hits, size=size, addr=addr, ops=list(zip(ops[0::2], ops[1::2])), code=code))
    return frags


def disasm(code: bytes, vma: int) -> list[tuple[int, str]]:
    with tempfile.NamedTemporaryFile(suffix=".bin") as f:
        f.write(code)
        f.flush()
        out = subprocess.run(
            [str(OBJDUMP), "-D", "-b", "binary", "-m", "arm", "--adjust-vma", hex(vma), f.name],
            capture_output=True, text=True, check=True,
        ).stdout
    result = []
    for line in out.splitlines():
        m = re.match(r"\s*([0-9a-f]+):\s+[0-9a-f]{8}\s+(.*)", line)
        if m:
            result.append((int(m.group(1), 16), m.group(2).split(";")[0].strip()))
    return result


def mips_name(op: int) -> str:
    instr = rabbitizer.Instruction(op)
    return instr.getOpcodeName()


def classify(text: str) -> str:
    mnem = text.split()[0]
    if "[ip" in text and mnem.startswith("str"):
        return "st_state"
    if "[ip" in text and mnem.startswith("ldr"):
        return "ld_state"
    if "[pc" in text and mnem.startswith("ldr"):
        return "ld_literal"
    if "sl]" in text and mnem.startswith(("ldr", "str")):
        return "mem_rdram"
    if mnem.startswith(("blx", "bl")):
        return "call"
    if mnem.startswith("b"):
        return "branch"
    if mnem.startswith(("ldr", "str", "ldm", "stm", "push", "pop")):
        return "mem_other"
    if mnem.startswith("v"):
        return "vfp"
    if mnem.startswith("cmp") or mnem.startswith("tst"):
        return "cmp"
    if mnem.startswith("mov") and "asr #31" in text:
        return "sign_ext"
    if mnem.startswith("mov"):
        return "mov"
    return "alu"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", type=Path)
    ap.add_argument("--show", type=int, default=0, help="print the N hottest fragments")
    ap.add_argument("--fragment", type=lambda s: int(s, 16), default=None)
    args = ap.parse_args()

    frags = parse(args.dump)
    total_weight = 0
    per_op_arm = collections.Counter()      # mips op name -> weighted ARM instrs
    per_op_count = collections.Counter()    # mips op name -> weighted executions
    per_class = collections.Counter()
    guest_total = 0
    prologue = 0
    for frag in frags:
        lines = disasm(frag["code"], frag["addr"])
        ops = frag["ops"]
        hits = frag["hits"]
        offsets = [off for _op, off in ops] + [frag["size"]]
        # Instructions before the first op: hit counter, loop setup.
        start_of = {}
        for i, (op, off) in enumerate(ops):
            start_of[i] = (off, offsets[i + 1])
        first = offsets[0] if ops else frag["size"]
        prologue += hits * first // 4
        literal_start = frag["size"]
        for i, (op, _off) in enumerate(ops):
            lo, hi = start_of[i]
            name = mips_name(op)
            n = 0
            for addr, text in lines:
                rel = addr - frag["addr"]
                if lo <= rel < hi:
                    n += 1
                    per_class[classify(text)] += hits
            per_op_arm[name] += n * hits
            per_op_count[name] += hits
            guest_total += hits
        frag["arm"] = frag["size"] // 4
        total_weight += hits * frag["size"] // 4

    print(f"fragments: {len(frags)}  weighted ARM words (main path, all exits): {total_weight}")
    print(f"guest instructions (weighted, static trace length): {guest_total}")
    print(f"ARM words per guest op (static, incl. exits/literals): {total_weight / max(guest_total, 1):.2f}")
    print()
    print("ARM instructions by class (weighted):")
    tot = sum(per_class.values())
    for k, v in per_class.most_common():
        print(f"  {k:12s} {100 * v / tot:5.1f}%")
    print()
    print("per MIPS op: share of guest ops, ARM instrs per op, share of ARM instrs")
    arm_tot = sum(per_op_arm.values())
    for name, cnt in per_op_count.most_common(45):
        print(f"  {name:10s} {100 * cnt / guest_total:5.1f}%  {per_op_arm[name] / cnt:5.2f}  {100 * per_op_arm[name] / arm_tot:5.1f}%")

    show = sorted(frags, key=lambda f: f["hits"] * f["size"], reverse=True)
    if args.fragment is not None:
        show = [f for f in frags if f["pc"] == args.fragment]
    elif args.show:
        show = show[: args.show]
    else:
        show = []
    for frag in show:
        print()
        print(f"=== fragment {frag['pc']:08x} hits={frag['hits']} bytes={frag['size']} ops={len(frag['ops'])}")
        lines = disasm(frag["code"], frag["addr"])
        op_at = {off: op for op, off in frag["ops"]}
        for addr, text in lines:
            rel = addr - frag["addr"]
            if rel in op_at:
                instr = rabbitizer.Instruction(op_at[rel])
                print(f"        ; {instr.disassemble()}")
            print(f"  {rel:5x}: {text}")


if __name__ == "__main__":
    main()

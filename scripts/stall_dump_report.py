#!/usr/bin/env python3
"""Report the guest state captured by `STALLDUMP=1` at the Old 3DS watchdog.

The dump is: header, raw SCPUState, all RDRAM (see apply_stalldump_diag.py).
This tool prints the emulated CPU registers and the libultra thread/message
queue state. The 2026-10-26 log shows VI progress after rendering stopped;
whether worker threads are blocked and which wakeup is missing remain unknown.

Guest addresses are virtual: 0x80000000..0x807FFFFF map to RDRAM offset
(addr - 0x80000000). The dump contains Daedalus's native word-swapped RDRAM,
not a byte-order-normalized N64 image. Thread and queue layouts come from the game's libultra
headers (`hm64-decomp/lib/libultra/include/PR/os_thread.h` and
`os_message.h`):

  OSThread: next@0x00 priority@0x04 queue@0x08 tlnext@0x0C state@0x10
            flags@0x12 id@0x14 fp@0x18 thprof@0x1C context@0x20
            context.pc@0x11C context.sp@0xF0 context.ra@0x100
  OSMesgQueue: mtqueue@0x00 fullqueue@0x04 validCount@0x08 first@0x0C
               msgCount@0x10 msg@0x14

usage: stall_dump_report.py DUMP [--symbols TOML] [--data-symbols TOML]
                                 [--ram-out PATH] [--threads]
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path

RAM_BASE = 0x80000000
RAM_SIZE_MAX = 8 * 1024 * 1024
HDR_FMT = "<14I28s4xQ"
HDR_MAGIC = 0x5334364E  # 'N64S'
CPU_STATE_OFFSETS = {
    "CurrentPC": 0x280, "TargetPC": 0x284, "Delay": 0x288, "StuffToDo": 0x28C,
    "NumEvents": 0x2F0,
}
CP0_NAMES = {
    8: "BadVAddr", 9: "Count", 11: "Compare", 12: "SR", 13: "Cause",
    14: "EPC", 15: "PRId", 16: "Config",
}
GPR_NAMES = ("zero", "at", "v0", "v1", "a0", "a1", "a2", "a3",
             "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7",
             "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7",
             "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra")
THREAD_STATE = {1: "STOPPED", 2: "RUNNABLE", 4: "RUNNING", 8: "WAITING"}
THREAD_CONTEXT_REGS = (
    ("at", 0x00), ("v0", 0x08), ("v1", 0x10), ("a0", 0x18),
    ("a1", 0x20), ("a2", 0x28), ("a3", 0x30),
    ("t0", 0x38), ("t1", 0x40), ("t2", 0x48), ("t3", 0x50),
    ("t4", 0x58), ("t5", 0x60), ("t6", 0x68), ("t7", 0x70),
    ("s0", 0x78), ("s1", 0x80), ("s2", 0x88), ("s3", 0x90),
    ("s4", 0x98), ("s5", 0xA0), ("s6", 0xA8), ("s7", 0xB0),
    ("t8", 0xB8), ("t9", 0xC0), ("gp", 0xC8), ("sp", 0xD0),
    ("s8", 0xD8), ("ra", 0xE0), ("lo", 0xE8), ("hi", 0xF0),
)
THREAD_CTX = 0x20
THREAD_PC = THREAD_CTX + 0xFC
THREAD_SR = THREAD_CTX + 0xF8
THREAD_CAUSE = THREAD_CTX + 0x100

SYM_RE = re.compile(
    r'name\s*=\s*"([^"]+)"\s*,\s*vram\s*=\s*(0x[0-9A-Fa-f]+)'
    r'(?:\s*,\s*size\s*=\s*(0x[0-9A-Fa-f]+))?')
DATA_SYM_RE = re.compile(r'^\s*([A-Za-z_]\w*)\s*=\s*(0x[0-9A-Fa-f]+)\s*;', re.M)


class Symbols:
    def __init__(self, paths: list[Path]):
        self.entries: list[tuple[int, int, str]] = []
        for path in paths:
            if not path or not path.is_file():
                continue
            source = path.read_text(errors="replace")
            for name, vram, size in SYM_RE.findall(source):
                self.entries.append((int(vram, 16), int(size, 16) if size else 0, name))
            for name, vram in DATA_SYM_RE.findall(source):
                self.entries.append((int(vram, 16), 0, name))
        self.entries.sort()
        self.by_addr = {a: (s, n) for a, s, n in self.entries}

    def name(self, addr: int) -> str | None:
        if addr in self.by_addr:
            return self.by_addr[addr][1]
        best = None
        for start, size, name in self.entries:
            if start <= addr and (size == 0 or addr < start + size):
                if best is None or start >= best[0]:
                    best = (start, name)
            elif start > addr:
                break
        if best is None:
            return None
        return best[1] if best[0] == addr else f"{best[1]}+0x{addr - best[0]:X}"


class GuestRam:
    def __init__(self, data: bytes):
        self.data = data

    def off(self, addr: int, length: int = 1) -> int:
        if not self.inside(addr, length):
            raise ValueError(f"guest RDRAM address out of range: 0x{addr:08X}")
        return addr - RAM_BASE

    def u8(self, addr: int) -> int:
        return self.data[self.off(addr) ^ 3]

    def u16(self, addr: int) -> int:
        return struct.unpack_from("<H", self.data, self.off(addr, 2) ^ 2)[0]

    def u32(self, addr: int) -> int:
        return struct.unpack_from("<I", self.data, self.off(addr, 4))[0]

    def u64(self, addr: int) -> int:
        offset = self.off(addr, 8)
        high, low = struct.unpack_from("<II", self.data, offset)
        return (high << 32) | low

    def inside(self, addr: int, length: int = 1) -> bool:
        return RAM_BASE <= addr and addr + length <= RAM_BASE + len(self.data)


def read_thread(ram: GuestRam, addr: int) -> dict | None:
    if not ram.inside(addr, 0x1B0):
        return None
    t = {
        "addr": addr,
        "next": ram.u32(addr + 0x00),
        "priority": ram.u32(addr + 0x04),
        "queue": ram.u32(addr + 0x08),
        "tlnext": ram.u32(addr + 0x0C),
        "state": ram.u16(addr + 0x10),
        "flags": ram.u16(addr + 0x12),
        "id": ram.u32(addr + 0x14),
        "fp": ram.u32(addr + 0x18),
        "pc": ram.u32(addr + THREAD_PC),
        "sr": ram.u32(addr + THREAD_SR),
        "cause": ram.u32(addr + THREAD_CAUSE),
    }
    for name, off in THREAD_CONTEXT_REGS:
        t[name] = ram.u64(addr + THREAD_CTX + off)
    return t


def plausible_thread(ram: GuestRam, addr: int) -> bool:
    t = read_thread(ram, addr)
    if t is None or addr & 0x7:
        return False
    if t["state"] not in THREAD_STATE or t["priority"] > 255 or t["id"] > 64:
        return False
    if t["next"] != 0 and not ram.inside(t["next"], 0x1B0):
        return False
    if t["tlnext"] != 0 and not ram.inside(t["tlnext"], 0x1B0):
        return False
    # Saved MIPS64 pointers are commonly sign extended from KSEG0.
    sp = t["sp"]
    if not (sp == 0 or (sp >> 32 in (0, 0xFFFFFFFF) and ram.inside(sp & 0xFFFFFFFF))):
        return False
    if not (ram.inside(t["pc"]) or t["pc"] == 0):
        return False
    return True


def read_queue(ram: GuestRam, addr: int) -> dict | None:
    if not ram.inside(addr, 0x18):
        return None
    q = {
        "addr": addr,
        "mtqueue": ram.u32(addr + 0x00),
        "fullqueue": ram.u32(addr + 0x04),
        "validCount": ram.u32(addr + 0x08),
        "first": ram.u32(addr + 0x0C),
        "msgCount": ram.u32(addr + 0x10),
        "msg": ram.u32(addr + 0x14),
    }
    if not (0 <= q["validCount"] <= q["msgCount"] <= 1024):
        return None
    if q["validCount"] and not ram.inside(q["msg"], 4 * max(q["msgCount"], 1)):
        return None
    for t in (q["mtqueue"], q["fullqueue"]):
        # libultra uses __osThreadTail (next=0, priority=-1) as an empty-list
        # sentinel. It is not a complete OSThread and must not be decoded as one.
        sentinel = ram.inside(t, 8) and ram.u32(t) == 0 and ram.u32(t + 4) == 0xFFFFFFFF
        if t and not sentinel and not plausible_thread(ram, t):
            return None
    return q


def chain(ram: GuestRam, head: int, limit: int = 64, link: str = "next") -> list[int]:
    out = []
    seen = set()
    while head and head not in seen and len(out) < limit:
        if not plausible_thread(ram, head):
            break
        seen.add(head)
        out.append(head)
        t = read_thread(ram, head)
        if t is None:
            break
        head = t[link]
    return out


def fmt_thread(ram: GuestRam, t: dict, sym: Symbols) -> str:
    state = THREAD_STATE.get(t["state"], f"0x{t['state']:X}")
    pc = f"0x{t['pc']:08X}"
    if sym and t["pc"]:
        pc += f" ({sym.name(t['pc'])})"
    queue = f"0x{t['queue']:08X}"
    if t["queue"] and sym:
        queue += f" ({sym.name(t['queue'])})"
    return (f"  0x{t['addr']:08X} id={t['id']} prio={t['priority']:3d} {state:8s}"
            f" pc={pc}\n      sp=0x{t['sp']:016X} ra=0x{t['ra']:016X} queue={queue}"
            f" sr=0x{t['sr']:08X} cause=0x{t['cause']:08X}")


def queue_addr(ram: GuestRam, addr: int) -> int:
    """Resolve a thread's `queue` field to an OSMesgQueue address, if any."""
    if addr == 0:
        return 0
    for delta in (0, 4):
        if read_queue(ram, addr - delta) is not None:
            return addr - delta
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dump", type=Path)
    ap.add_argument("--symbols", "--data-symbols", dest="symbols", type=Path,
                    action="append", default=[],
                    help="N64Recomp symbols TOML or decomp symbol_addrs.txt (repeatable)")
    ap.add_argument("--ram-out", type=Path, help="write RDRAM to this file")
    ap.add_argument("--threads", action="store_true", help="scan all RDRAM for thread structs")
    args = ap.parse_args()

    blob = args.dump.read_bytes()
    if len(blob) < struct.calcsize(HDR_FMT):
        print("not a stall dump: too short", file=sys.stderr)
        return 2
    (magic, version, hdr_size, ram_size, cpu_size, vi, vint, mi_intr, mi_mask,
     gfx_flight, audio_busy, gfx_phase, cpu_phase, reason_len, reason,
     tick) = struct.unpack_from(HDR_FMT, blob, 0)
    if magic != HDR_MAGIC:
        print(f"bad magic 0x{magic:08X}", file=sys.stderr)
        return 2
    cpu = blob[hdr_size:hdr_size + cpu_size]
    ram_bytes = blob[hdr_size + cpu_size:hdr_size + cpu_size + ram_size]
    if len(ram_bytes) != ram_size:
        print(f"short RDRAM: {len(ram_bytes)} of {ram_size}", file=sys.stderr)
        return 2
    ram = GuestRam(ram_bytes)

    sym = Symbols([p for p in args.symbols if p])
    print(f"dump {args.dump}: version={version} vi={vi} reason={reason[:reason_len].decode(errors='replace')}")
    print(f"  ram={ram_size} cpu_state={cpu_size} mil_intr=0x{mi_intr:08X} mi_mask=0x{mi_mask:08X}"
          f" gfx_task={gfx_flight} audio_busy={audio_busy} gfx_phase={gfx_phase} cpu_phase={cpu_phase}")
    if cpu_size < 0x2F4:
        print(f"  cpu state too small for SCPUState: {cpu_size}", file=sys.stderr)
        return 2

    pc = struct.unpack_from("<I", cpu, CPU_STATE_OFFSETS["CurrentPC"])[0]
    target = struct.unpack_from("<I", cpu, CPU_STATE_OFFSETS["TargetPC"])[0]
    stuff = struct.unpack_from("<I", cpu, CPU_STATE_OFFSETS["StuffToDo"])[0]
    nevents = struct.unpack_from("<I", cpu, CPU_STATE_OFFSETS["NumEvents"])[0]
    print(f"  pc=0x{pc:08X} target=0x{target:08X} stuff=0x{stuff:08X} events={nevents}")
    events = []
    for i in range(min(nevents, 8)):
        count, etype = struct.unpack_from("<iI", cpu, 0x2B0 + 8 * i)
        events.append(f"{etype}:{count}")
    print("  event queue: " + (" ".join(events) if events else "(empty)"))
    print("  GPRs:")
    for i in range(0, 32, 4):
        cols = " ".join(f"{GPR_NAMES[i + j]}={struct.unpack_from('<Q', cpu, 8 * (i + j))[0]:016X}"
                        for j in range(4))
        print("   " + cols)
    ctl = {name: struct.unpack_from("<I", cpu, 0x100 + 4 * idx)[0]
           for idx, name in CP0_NAMES.items()}
    print("  CP0: " + " ".join(f"{k}=0x{v:08X}" for k, v in ctl.items()))

    if args.ram_out:
        args.ram_out.write_bytes(ram_bytes)
        print(f"  RDRAM written to {args.ram_out}")

    # Global libultra state.
    for name in ("__osRunQueue", "__osActiveQueue",
                 "__osRunningThread", "__osFaultedThread"):
        addr = next((a for a, (_s, n) in sym.by_addr.items() if n == name), None)
        if addr is None:
            continue
        val = ram.u32(addr)
        extra = ""
        if val and ram.inside(val, 0x1B0) and plausible_thread(ram, val):
            t = read_thread(ram, val)
            extra = f" -> thread id={t['id']} {THREAD_STATE.get(t['state'], t['state'])}"
        print(f"  {name}@0x{addr:08X} = 0x{val:08X}{extra}")

    print("  threads:")
    seen_threads: list[int] = []
    for addr, (_s, name) in sorted(sym.by_addr.items()):
        if not re.search(r"Thread(_|$)", name):
            continue
        t = read_thread(ram, addr)
        if t is None or not plausible_thread(ram, addr):
            continue
        seen_threads.append(addr)
        print(f"  {name}:")
        print(fmt_thread(ram, t, sym))

    all_threads = list(dict.fromkeys(seen_threads))
    active = next((a for a, (_s, n) in sym.by_addr.items() if n == "__osActiveQueue"), 0)
    if active:
        head = ram.u32(active)
        for addr in chain(ram, head, link="tlnext"):
            if addr not in all_threads:
                all_threads.append(addr)
    running = next((a for a, (_s, n) in sym.by_addr.items() if n == "__osRunningThread"), 0)
    if running:
        addr = ram.u32(running)
        if addr and addr not in all_threads:
            all_threads.append(addr)

    print("  blocked message queues:")
    queues: dict[int, list[int]] = {}
    for addr in all_threads:
        t = read_thread(ram, addr)
        if t is None:
            continue
        q = queue_addr(ram, t["queue"])
        if q:
            queues.setdefault(q, []).append(addr)
    if not queues:
        print("   (none: no thread is on a message queue)")
    for qaddr, members in sorted(queues.items()):
        q = read_queue(ram, qaddr)
        if q is None:
            continue
        name = next((n for a, (_s, n) in sym.by_addr.items() if a == qaddr), "")
        print(f"   queue 0x{qaddr:08X} {name}: valid={q['validCount']}"
              f" first={q['first']} of={q['msgCount']} msg=0x{q['msg']:08X}")
        for field in ("mtqueue", "fullqueue"):
            for i, addr in enumerate(chain(ram, q[field])):
                t = read_thread(ram, addr)
                label = "recv" if field == "mtqueue" else "send"
                print(f"     {label} {i}: 0x{addr:08X} id={t['id']} prio={t['priority']}"
                      f" pc=0x{t['pc']:08X} ({sym.name(t['pc']) or '?'})")

    if args.threads:
        print("  RDRAM thread-struct scan:")
        found = []
        for off in range(0, len(ram_bytes) - 0x1B0, 8):
            addr = RAM_BASE + off
            if addr in all_threads:
                continue
            if plausible_thread(ram, addr):
                found.append(addr)
        for addr in found:
            t = read_thread(ram, addr)
            print(f"  0x{addr:08X} id={t['id']} prio={t['priority']}"
                  f" {THREAD_STATE.get(t['state'], t['state'])}"
                  f" pc=0x{t['pc']:08X} ({sym.name(t['pc']) or '?'})")
        if not found:
            print("   (none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

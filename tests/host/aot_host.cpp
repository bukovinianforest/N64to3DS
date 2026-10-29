// Host-side semantic test for generated n64to3ds AOT blocks.
//
// This is deliberately *not* an emulator.  It compiles the exact generated
// translation unit against the N64TO3DS_HOST_TEST ABI and compares every block
// with an independent, small MIPS interpreter.  The reference interpreter is
// written directly against the instruction encoding, not against codegen.py,
// so a generator bug cannot silently agree with itself.
//
// The production runtime has one authoritative CPU state (Daedalus' gGPR /
// gCPUState).  This harness has one test CPU state and two execution paths.

#include "AOTGeneratedCommon.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <random>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

u64 n64to3ds_host_gpr[32];
u64 n64to3ds_host_lo;
u64 n64to3ds_host_hi;
u32 n64to3ds_host_pc;
bool n64to3ds_host_job;

extern "C" const AOTBlockDescriptor *const g_n64to3ds_aot_blocks;
extern "C" const u32 g_n64to3ds_aot_block_count;

namespace
{
constexpr u32 kMemorySize = 8u * 1024u * 1024u;
constexpr u32 kMemoryMask = kMemorySize - 1u;

std::vector<u8> g_memory;
std::unordered_map<u32, u32> g_words;

struct WriteRecord
{
    u32 address;
    u32 size;
    u64 value;
    u32 physical;
    std::array<u8, 8> old_bytes;

    bool operator==(const WriteRecord & rhs) const
    {
        return address == rhs.address && size == rhs.size && value == rhs.value;
    }
};

std::vector<WriteRecord> g_writes;

void RecordWrite(u32 address, u32 size, u64 value, u32 physical)
{
    WriteRecord record{};
    record.address = address & kMemoryMask;
    record.size = size;
    record.value = value;
    record.physical = physical;
    std::memcpy(record.old_bytes.data(), g_memory.data() + physical, size);
    g_writes.push_back(record);
}

void RestoreWrites()
{
    for (auto it = g_writes.rbegin(); it != g_writes.rend(); ++it)
        std::memcpy(g_memory.data() + it->physical, it->old_bytes.data(), it->size);
    g_writes.clear();
}

u32 PhysicalAddress(u32 address)
{
    return address & kMemoryMask;
}

template <typename T>
T ReadMemory(u32 physical)
{
    T value{};
    std::memcpy(&value, g_memory.data() + physical, sizeof(T));
    return value;
}

template <typename T>
void WriteMemory(u32 physical, T value)
{
    std::memcpy(g_memory.data() + physical, &value, sizeof(T));
}

u64 SwapHalves(u64 value)
{
    return (value >> 32) | (value << 32);
}

u32 FetchWord(u32 pc)
{
    const auto it = g_words.find(pc);
    if (it == g_words.end())
    {
        std::fprintf(stderr, "reference interpreter: no instruction word for 0x%08X\n", pc);
        std::exit(2);
    }
    return it->second;
}

u32 SignExtend16(u32 value)
{
    return static_cast<u32>(static_cast<s32>(static_cast<s16>(value)));
}

u64 SignExtend64(u32 value)
{
    return static_cast<u64>(static_cast<s64>(static_cast<s32>(value)));
}

s64 SignExtend16To64(u32 value)
{
    return static_cast<s64>(static_cast<s16>(value));
}

u32 Add32(u32 a, u32 b)
{
    return static_cast<u32>(a + b);
}

s32 Sra32(s32 value, u32 shift)
{
    if (shift == 0)
        return value;
    const u32 result = static_cast<u32>(value) >> shift;
    return static_cast<s32>(result | (value < 0 ? (~0u << (32 - shift)) : 0u));
}

s64 Sra64(s64 value, u32 shift)
{
    if (shift == 0)
        return value;
    const u64 result = static_cast<u64>(value) >> shift;
    return static_cast<s64>(result | (value < 0 ? (~0ull << (64 - shift)) : 0ull));
}

struct DivResult
{
    bool changed;
    u64 lo;
    u64 hi;
};

DivResult DivideSigned(s32 dividend, s32 divisor)
{
    if (divisor == 0)
        return {false, 0, 0};
    if (dividend == static_cast<s32>(0x80000000u) && divisor == -1)
        return {true, SignExtend64(0x80000000u), 0};
    return {true, SignExtend64(static_cast<u32>(dividend / divisor)),
            SignExtend64(static_cast<u32>(dividend % divisor))};
}

DivResult DivideUnsigned(u32 dividend, u32 divisor)
{
    if (divisor == 0)
        return {false, 0, 0};
    return {true, SignExtend64(dividend / divisor), SignExtend64(dividend % divisor)};
}

// Execute one non-control-flow instruction.  Returns false if the word is not
// part of the supported subset (which would make the test inconclusive).
bool ExecuteOne(u32 pc, u32 word)
{
    (void)pc;
    const u32 op = (word >> 26) & 0x3F;
    const u32 rs = (word >> 21) & 0x1F;
    const u32 rt = (word >> 16) & 0x1F;
    const u32 rd = (word >> 11) & 0x1F;
    const u32 sa = (word >> 6) & 0x1F;
    const u32 funct = word & 0x3F;
    const u32 imm = word & 0xFFFF;

    auto & gpr = n64to3ds_host_gpr;
    const u32 rs32 = static_cast<u32>(gpr[rs]);
    const u32 rt32 = static_cast<u32>(gpr[rt]);
    const s64 rs64 = static_cast<s64>(gpr[rs]);
    const s64 rt64 = static_cast<s64>(gpr[rt]);
    const u32 address = Add32(rs32, SignExtend16(imm));

    auto set_s32 = [&](u32 reg, u32 value) {
        if (reg != 0)
            gpr[reg] = SignExtend64(value);
    };
    auto set_u64 = [&](u32 reg, u64 value) {
        if (reg != 0)
            gpr[reg] = value;
    };

    if (op == 0x00)
    {
        switch (funct)
        {
        case 0x00: set_s32(rd, rt32 << sa); return true;                 // sll
        case 0x02: set_s32(rd, rt32 >> sa); return true;                 // srl
        case 0x03: set_s32(rd, static_cast<u32>(Sra32(static_cast<s32>(rt32), sa))); return true;
        case 0x04: set_s32(rd, rt32 << (rs32 & 31)); return true;        // sllv
        case 0x06: set_s32(rd, rt32 >> (rs32 & 31)); return true;        // srlv
        case 0x07: set_s32(rd, static_cast<u32>(Sra32(static_cast<s32>(rt32), rs32 & 31))); return true;
        case 0x08: // jr handled by caller
        case 0x09: return false;
        case 0x0F: return true;                                          // sync
        case 0x10: set_u64(rd, n64to3ds_host_hi); return true;           // mfhi
        case 0x11: n64to3ds_host_hi = gpr[rs]; return true;              // mthi
        case 0x12: set_u64(rd, n64to3ds_host_lo); return true;           // mflo
        case 0x13: n64to3ds_host_lo = gpr[rs]; return true;              // mtlo
        case 0x18: // mult
        {
            const s64 product = static_cast<s64>(static_cast<s32>(rs32)) *
                                static_cast<s64>(static_cast<s32>(rt32));
            n64to3ds_host_lo = SignExtend64(static_cast<u32>(product));
            n64to3ds_host_hi = SignExtend64(static_cast<u32>(product >> 32));
            return true;
        }
        case 0x19: // multu
        {
            const u64 product = static_cast<u64>(rs32) * static_cast<u64>(rt32);
            n64to3ds_host_lo = SignExtend64(static_cast<u32>(product));
            n64to3ds_host_hi = SignExtend64(static_cast<u32>(product >> 32));
            return true;
        }
        case 0x1A: // div
        {
            const DivResult result = DivideSigned(static_cast<s32>(rs32), static_cast<s32>(rt32));
            if (result.changed)
            {
                n64to3ds_host_lo = result.lo;
                n64to3ds_host_hi = result.hi;
            }
            return true;
        }
        case 0x1B: // divu
        {
            const DivResult result = DivideUnsigned(rs32, rt32);
            if (result.changed)
            {
                n64to3ds_host_lo = result.lo;
                n64to3ds_host_hi = result.hi;
            }
            return true;
        }
        case 0x20: set_s32(rd, Add32(rs32, rt32)); return true;          // add
        case 0x21: set_s32(rd, Add32(rs32, rt32)); return true;          // addu
        case 0x22: set_s32(rd, Add32(rs32, 0u - rt32)); return true;
        case 0x23: set_s32(rd, Add32(rs32, 0u - rt32)); return true;
        case 0x24: set_u64(rd, gpr[rs] & gpr[rt]); return true;
        case 0x25: set_u64(rd, gpr[rs] | gpr[rt]); return true;
        case 0x26: set_u64(rd, gpr[rs] ^ gpr[rt]); return true;
        case 0x27: set_u64(rd, ~(gpr[rs] | gpr[rt])); return true;
        case 0x2A: set_u64(rd, rs64 < rt64 ? 1 : 0); return true;
        case 0x2B: set_u64(rd, gpr[rs] < gpr[rt] ? 1 : 0); return true;
        case 0x2C: set_u64(rd, static_cast<u64>(rs64 + rt64)); return true; // dadd
        case 0x2D: set_u64(rd, gpr[rs] + gpr[rt]); return true;             // daddu
        case 0x2E: set_u64(rd, static_cast<u64>(rs64 - rt64)); return true; // dsub
        case 0x2F: set_u64(rd, gpr[rs] - gpr[rt]); return true;             // dsubu
        case 0x38: set_u64(rd, gpr[rt] << sa); return true;                 // dsll
        case 0x3A: set_u64(rd, gpr[rt] >> sa); return true;                 // dsrl
        case 0x3B: set_u64(rd, static_cast<u64>(Sra64(rt64, sa))); return true;
        case 0x3C: set_u64(rd, gpr[rt] << (32 + sa)); return true;          // dsll32
        case 0x3E: set_u64(rd, gpr[rt] >> (32 + sa)); return true;          // dsrl32
        case 0x3F: set_u64(rd, static_cast<u64>(Sra64(rt64, 32 + sa))); return true;
        default: return false;
        }
    }

    if (op == 0x01)
    {
        // bltz/bgez/bltzal/bgezal are handled by the caller.
        return false;
    }

    if (op == 0x08 || op == 0x09) // addi/addiu
    {
        set_s32(rt, Add32(rs32, SignExtend16(imm)));
        return true;
    }
    if (op == 0x0A) { set_u64(rt, rs64 < SignExtend16To64(imm) ? 1 : 0); return true; }
    if (op == 0x0B) { set_u64(rt, gpr[rs] < static_cast<u64>(SignExtend16To64(imm)) ? 1 : 0); return true; }
    if (op == 0x0C) { set_u64(rt, gpr[rs] & imm); return true; }
    if (op == 0x0D) { set_u64(rt, gpr[rs] | imm); return true; }
    if (op == 0x0E) { set_u64(rt, gpr[rs] ^ imm); return true; }
    if (op == 0x0F) { set_s32(rt, (imm << 16)); return true; }
    if (op == 0x18) { set_u64(rt, static_cast<u64>(rs64 + SignExtend16To64(imm))); return true; }
    if (op == 0x19) { set_u64(rt, gpr[rs] + static_cast<u64>(SignExtend16To64(imm))); return true; }

    if (op == 0x20) { set_u64(rt, SignExtend64(static_cast<u32>(static_cast<s8>(AOT_HostRead8(address))))); return true; }
    if (op == 0x21) { set_u64(rt, SignExtend64(static_cast<u32>(static_cast<s16>(AOT_HostRead16(address))))); return true; }
    if (op == 0x23) { set_s32(rt, AOT_HostRead32(address)); return true; }
    if (op == 0x24) { set_u64(rt, AOT_HostRead8(address)); return true; }
    if (op == 0x25) { set_u64(rt, AOT_HostRead16(address)); return true; }
    if (op == 0x27) { set_u64(rt, AOT_HostRead32(address)); return true; }
    if (op == 0x37) { set_u64(rt, AOT_HostRead64(address)); return true; }
    if (op == 0x28) { AOT_HostWrite8(address, static_cast<u8>(rt32)); return true; }
    if (op == 0x29) { AOT_HostWrite16(address, static_cast<u16>(rt32)); return true; }
    if (op == 0x2B) { AOT_HostWrite32(address, rt32); return true; }
    if (op == 0x3F) { AOT_HostWrite64(address, gpr[rt]); return true; }

    return false;
}

struct ReferenceResult
{
    u32 next_pc;
};

ReferenceResult ExecuteReferenceBlock(const AOTBlockDescriptor & block)
{
    u32 pc = block.start;
    for (u32 index = 0; index < block.instruction_count; ++index)
    {
        const u32 word = FetchWord(pc);
        const u32 op = (word >> 26) & 0x3F;
        const u32 rs = (word >> 21) & 0x1F;
        const u32 rt = (word >> 16) & 0x1F;
        const u32 rd = (word >> 11) & 0x1F;
        const u32 imm = word & 0xFFFF;
        const u32 target = word & 0x03FFFFFF;

        if (op == 0x00 && (word & 0x3F) == 0x08) // jr
        {
            const u32 next = static_cast<u32>(n64to3ds_host_gpr[rs]);
            const u32 delay_pc = pc + 4;
            if (!ExecuteOne(delay_pc, FetchWord(delay_pc)))
            {
                std::fprintf(stderr, "reference: unsupported delay slot at 0x%08X\n", delay_pc);
                std::exit(2);
            }
            return {next};
        }
        if (op == 0x00 && (word & 0x3F) == 0x09) // jalr
        {
            const u32 next = static_cast<u32>(n64to3ds_host_gpr[rs]);
            if (rd != 0)
                n64to3ds_host_gpr[rd] = SignExtend64(pc + 8);
            const u32 delay_pc = pc + 4;
            if (!ExecuteOne(delay_pc, FetchWord(delay_pc)))
            {
                std::fprintf(stderr, "reference: unsupported delay slot at 0x%08X\n", delay_pc);
                std::exit(2);
            }
            return {next};
        }
        if (op == 0x02 || op == 0x03) // j/jal
        {
            const u32 next = (pc & 0xF0000000u) | (target << 2);
            if (op == 0x03)
                n64to3ds_host_gpr[31] = SignExtend64(pc + 8);
            const u32 delay_pc = pc + 4;
            if (!ExecuteOne(delay_pc, FetchWord(delay_pc)))
            {
                std::fprintf(stderr, "reference: unsupported delay slot at 0x%08X\n", delay_pc);
                std::exit(2);
            }
            return {next};
        }
        if (op == 0x04 || op == 0x05 || op == 0x06 || op == 0x07)
        {
            bool taken = false;
            if (op == 0x04) taken = n64to3ds_host_gpr[rs] == n64to3ds_host_gpr[rt];
            if (op == 0x05) taken = n64to3ds_host_gpr[rs] != n64to3ds_host_gpr[rt];
            if (op == 0x06) taken = static_cast<s64>(n64to3ds_host_gpr[rs]) <= 0;
            if (op == 0x07) taken = static_cast<s64>(n64to3ds_host_gpr[rs]) > 0;
            const u32 next = taken ? Add32(pc + 4, SignExtend16(imm) << 2) : pc + 8;
            const u32 delay_pc = pc + 4;
            if (!ExecuteOne(delay_pc, FetchWord(delay_pc)))
            {
                std::fprintf(stderr, "reference: unsupported delay slot at 0x%08X\n", delay_pc);
                std::exit(2);
            }
            return {next};
        }
        if (op == 0x01)
        {
            const u32 regimm = rt;
            if (regimm <= 0x03 || regimm == 0x10 || regimm == 0x11)
            {
                const s64 value = static_cast<s64>(n64to3ds_host_gpr[rs]);
                bool taken = false;
                if (regimm == 0x00 || regimm == 0x10) taken = value < 0;
                if (regimm == 0x01 || regimm == 0x11) taken = value >= 0;
                if (regimm == 0x10 || regimm == 0x11)
                    n64to3ds_host_gpr[31] = SignExtend64(pc + 8);
                const u32 next = taken ? Add32(pc + 4, SignExtend16(imm) << 2) : pc + 8;
                const u32 delay_pc = pc + 4;
                if (!ExecuteOne(delay_pc, FetchWord(delay_pc)))
                {
                    std::fprintf(stderr, "reference: unsupported delay slot at 0x%08X\n", delay_pc);
                    std::exit(2);
                }
                return {next};
            }
        }

        if (!ExecuteOne(pc, word))
        {
            std::fprintf(stderr, "reference: unsupported instruction 0x%08X at 0x%08X\n", word, pc);
            std::exit(2);
        }
        pc += 4;
    }
    return {pc};
}

u64 NextRandom(std::mt19937_64 & rng)
{
    return rng();
}

struct State
{
    u64 gpr[32];
    u64 lo;
    u64 hi;
    u32 pc;
    std::vector<WriteRecord> writes;
};

State CaptureState(u32 pc)
{
    State state{};
    std::memcpy(state.gpr, n64to3ds_host_gpr, sizeof(state.gpr));
    state.lo = n64to3ds_host_lo;
    state.hi = n64to3ds_host_hi;
    state.pc = pc;
    state.writes = g_writes;
    return state;
}

void ResetCpu(const State & initial)
{
    std::memcpy(n64to3ds_host_gpr, initial.gpr, sizeof(initial.gpr));
    n64to3ds_host_lo = initial.lo;
    n64to3ds_host_hi = initial.hi;
    n64to3ds_host_pc = initial.pc;
    n64to3ds_host_job = false;
    g_writes.clear();
}

bool CompareStates(const State & aot, const State & ref, u32 block_start)
{
    if (aot.pc != ref.pc)
    {
        std::fprintf(stderr, "0x%08X: next PC differs: AOT 0x%08X ref 0x%08X\n",
                     block_start, aot.pc, ref.pc);
        return false;
    }
    if (aot.lo != ref.lo || aot.hi != ref.hi)
    {
        std::fprintf(stderr, "0x%08X: LO/HI differ: AOT %016llX/%016llX ref %016llX/%016llX\n",
                     block_start,
                     static_cast<unsigned long long>(aot.lo),
                     static_cast<unsigned long long>(aot.hi),
                     static_cast<unsigned long long>(ref.lo),
                     static_cast<unsigned long long>(ref.hi));
        return false;
    }
    for (u32 i = 0; i < 32; ++i)
    {
        if (aot.gpr[i] != ref.gpr[i])
        {
            std::fprintf(stderr, "0x%08X: $%u differs: AOT %016llX ref %016llX\n",
                         block_start, i,
                         static_cast<unsigned long long>(aot.gpr[i]),
                         static_cast<unsigned long long>(ref.gpr[i]));
            return false;
        }
    }
    if (aot.writes.size() != ref.writes.size())
    {
        std::fprintf(stderr, "0x%08X: write count differs: AOT %zu ref %zu\n",
                     block_start, aot.writes.size(), ref.writes.size());
        return false;
    }
    for (size_t i = 0; i < aot.writes.size(); ++i)
    {
        if (!(aot.writes[i] == ref.writes[i]))
        {
            std::fprintf(stderr,
                         "0x%08X: write %zu differs: AOT addr=0x%08X size=%u value=%016llX "
                         "ref addr=0x%08X size=%u value=%016llX\n",
                         block_start, i,
                         aot.writes[i].address, aot.writes[i].size,
                         static_cast<unsigned long long>(aot.writes[i].value),
                         ref.writes[i].address, ref.writes[i].size,
                         static_cast<unsigned long long>(ref.writes[i].value));
            return false;
        }
    }
    return true;
}

std::vector<u8> g_rom_bytes;

bool LoadRom(const char * rom_path)
{
    std::ifstream rom(rom_path, std::ios::binary);
    if (!rom)
    {
        std::fprintf(stderr, "cannot open ROM %s\n", rom_path);
        return false;
    }
    g_rom_bytes.assign((std::istreambuf_iterator<char>(rom)), std::istreambuf_iterator<char>());
    if (g_rom_bytes.size() < 0x1000)
    {
        std::fprintf(stderr, "ROM is too small\n");
        return false;
    }
    return true;
}

// Load exactly the words a block was proven from.  The descriptor's
// source_offset is authoritative, so blocks that share a virtual address under
// different mapping hypotheses do not overwrite each other's reference code.
bool LoadBlockWords(const AOTBlockDescriptor & block)
{
    g_words.clear();
    for (u32 i = 0; i < block.instruction_count; ++i)
    {
        const u64 offset64 = static_cast<u64>(block.source_offset) + static_cast<u64>(i) * 4ull;
        if (offset64 + 4 > g_rom_bytes.size())
        {
            std::fprintf(stderr,
                         "block 0x%08X (rom 0x%08X) is outside the ROM\n",
                         block.start,
                         block.source_offset);
            return false;
        }
        const size_t offset = static_cast<size_t>(offset64);
        const u32 word = (static_cast<u32>(g_rom_bytes[offset]) << 24) |
                         (static_cast<u32>(g_rom_bytes[offset + 1]) << 16) |
                         (static_cast<u32>(g_rom_bytes[offset + 2]) << 8) |
                         static_cast<u32>(g_rom_bytes[offset + 3]);
        g_words[block.start + i * 4] = word;
    }
    return true;
}

void FillMemory()
{
    u64 value = 0x9E3779B97F4A7C15ull;
    for (u32 i = 0; i < kMemorySize; ++i)
    {
        value ^= value << 13;
        value ^= value >> 7;
        value ^= value << 17;
        g_memory[i] = static_cast<u8>(value >> 24);
    }
}
} // namespace

u8 AOT_HostRead8(u32 address)
{
    const u32 physical = PhysicalAddress(address) ^ 3u;
    return g_memory[physical];
}

u16 AOT_HostRead16(u32 address)
{
    const u32 physical = PhysicalAddress(address) ^ 2u;
    return ReadMemory<u16>(physical);
}

u32 AOT_HostRead32(u32 address)
{
    return ReadMemory<u32>(PhysicalAddress(address));
}

u64 AOT_HostRead64(u32 address)
{
    return SwapHalves(ReadMemory<u64>(PhysicalAddress(address)));
}

void AOT_HostWrite8(u32 address, u8 value)
{
    const u32 physical = PhysicalAddress(address) ^ 3u;
    RecordWrite(address, 1, value, physical);
    g_memory[physical] = value;
}

void AOT_HostWrite16(u32 address, u16 value)
{
    const u32 physical = PhysicalAddress(address) ^ 2u;
    RecordWrite(address, 2, value, physical);
    WriteMemory<u16>(physical, value);
}

void AOT_HostWrite32(u32 address, u32 value)
{
    const u32 physical = PhysicalAddress(address);
    RecordWrite(address, 4, value, physical);
    WriteMemory<u32>(physical, value);
}

void AOT_HostWrite64(u32 address, u64 value)
{
    const u32 physical = PhysicalAddress(address);
    RecordWrite(address, 8, value, physical);
    WriteMemory<u64>(physical, SwapHalves(value));
}

int main(int argc, char ** argv)
{
    const char * rom_path = nullptr;
    u32 iterations = 200;

    for (int i = 1; i < argc; ++i)
    {
        const std::string arg = argv[i];
        if (arg == "--rom" && i + 1 < argc)
            rom_path = argv[++i];
        else if (arg == "--iterations" && i + 1 < argc)
            iterations = static_cast<u32>(std::strtoul(argv[++i], nullptr, 0));
        else
        {
            std::fprintf(stderr, "unknown argument: %s\n", arg.c_str());
            return 2;
        }
    }

    if (rom_path == nullptr)
    {
        std::fprintf(stderr, "usage: %s --rom <normalized.z64> [--iterations N]\n", argv[0]);
        return 2;
    }
    if (g_n64to3ds_aot_block_count == 0)
    {
        std::printf("no AOT blocks to test\n");
        return 0;
    }

    g_memory.assign(kMemorySize + 16, 0);
    FillMemory();
    if (!LoadRom(rom_path))
        return 2;

    std::mt19937_64 rng(0x6E3634746F334453ull);
    u64 tested = 0;

    for (u32 block_index = 0; block_index < g_n64to3ds_aot_block_count; ++block_index)
    {
        const AOTBlockDescriptor & block = g_n64to3ds_aot_blocks[block_index];
        if (!LoadBlockWords(block))
            return 2;
        for (u32 iteration = 0; iteration < iterations; ++iteration)
        {
            State initial{};
            initial.lo = NextRandom(rng);
            initial.hi = NextRandom(rng);
            for (u32 reg = 1; reg < 32; ++reg)
                initial.gpr[reg] = NextRandom(rng);
            initial.gpr[0] = 0;
            // Give several registers aligned pointers into the test memory so
            // load/store blocks are actually exercised.
            for (u32 reg : {4u, 5u, 6u, 7u, 28u, 29u, 30u, 31u})
                initial.gpr[reg] = static_cast<u64>((NextRandom(rng) & (kMemorySize - 8u)) & ~7ull);
            initial.pc = block.start;

            ResetCpu(initial);
            n64to3ds_host_job = false;
            const AOTBlockResult aot_result = block.function();
            State aot_state = CaptureState(aot_result.next_pc);
            RestoreWrites();

            ResetCpu(initial);
            const ReferenceResult ref_result = ExecuteReferenceBlock(block);
            State ref_state = CaptureState(ref_result.next_pc);
            RestoreWrites();

            if (aot_result.status != AOT_STATUS_OK)
            {
                std::fprintf(stderr, "0x%08X: AOT returned status %u\n", block.start, aot_result.status);
                return 1;
            }
            if (!CompareStates(aot_state, ref_state, block.start))
            {
                std::fprintf(stderr, "failed at block 0x%08X iteration %u\n", block.start, iteration);
                return 1;
            }
            ++tested;
        }
    }

    std::printf("AOT host semantic test passed: %u blocks, %llu executions\n",
                g_n64to3ds_aot_block_count,
                static_cast<unsigned long long>(tested));
    return 0;
}

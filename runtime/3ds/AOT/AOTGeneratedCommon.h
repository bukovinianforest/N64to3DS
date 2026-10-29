// Shared ABI between generated AOT blocks, the Daedalus 3DS runtime and the
// host semantic test harness.
//
// There is exactly one authoritative CPU state in the real runtime: Daedalus'
// gCPUState/gGPR.  The macros below are thin lvalues/helpers over that state;
// no CPU context is copied per block transition.

#pragma once

#include <cstdint>

#ifdef N64TO3DS_HOST_TEST

using u8 = std::uint8_t;
using u16 = std::uint16_t;
using u32 = std::uint32_t;
using u64 = std::uint64_t;
using s8 = std::int8_t;
using s16 = std::int16_t;
using s32 = std::int32_t;
using s64 = std::int64_t;

extern u64 n64to3ds_host_gpr[32];
extern u64 n64to3ds_host_lo;
extern u64 n64to3ds_host_hi;
extern u32 n64to3ds_host_pc;
extern bool n64to3ds_host_job;

u8 AOT_HostRead8(u32 address);
u16 AOT_HostRead16(u32 address);
u32 AOT_HostRead32(u32 address);
u64 AOT_HostRead64(u32 address);
void AOT_HostWrite8(u32 address, u8 value);
void AOT_HostWrite16(u32 address, u16 value);
void AOT_HostWrite32(u32 address, u32 value);
void AOT_HostWrite64(u32 address, u64 value);

#define AOT_GPR_U64(i) (n64to3ds_host_gpr[(i)])
#define AOT_GPR_S64(i) ((s64)n64to3ds_host_gpr[(i)])
#define AOT_GPR_U32(i) ((u32)n64to3ds_host_gpr[(i)])
#define AOT_GPR_S32(i) ((s32)(u32)n64to3ds_host_gpr[(i)])
#define AOT_LO_U64() (n64to3ds_host_lo)
#define AOT_HI_U64() (n64to3ds_host_hi)
#define AOT_SET_LO_U64(v) (n64to3ds_host_lo = (u64)(v))
#define AOT_SET_HI_U64(v) (n64to3ds_host_hi = (u64)(v))
#define AOT_SET_PC(pc) (n64to3ds_host_pc = (u32)(pc))
#define AOT_SET_DELAY_EXEC() ((void)0)
#define AOT_SET_DELAY_NONE() ((void)0)
#define AOT_HAS_PENDING_JOB() (n64to3ds_host_job)

#define AOT_READ8(address) AOT_HostRead8(address)
#define AOT_READ16(address) AOT_HostRead16(address)
#define AOT_READ32(address) AOT_HostRead32(address)
#define AOT_READ64(address) AOT_HostRead64(address)
#define AOT_WRITE8(address, value) AOT_HostWrite8((address), (value))
#define AOT_WRITE16(address, value) AOT_HostWrite16((address), (value))
#define AOT_WRITE32(address, value) AOT_HostWrite32((address), (value))
#define AOT_WRITE64(address, value) AOT_HostWrite64((address), (value))

#else

#include "Core/CPU.h"
#include "Core/Memory.h"

#define AOT_GPR_U64(i) (gGPR[(i)]._u64)
#define AOT_GPR_S64(i) (gGPR[(i)]._s64)
#define AOT_GPR_U32(i) (gGPR[(i)]._u32_0)
#define AOT_GPR_S32(i) (gGPR[(i)]._s32_0)
#define AOT_LO_U64() (gCPUState.MultLo._u64)
#define AOT_HI_U64() (gCPUState.MultHi._u64)
#define AOT_SET_LO_U64(v) (gCPUState.MultLo._u64 = (u64)(v))
#define AOT_SET_HI_U64(v) (gCPUState.MultHi._u64 = (u64)(v))
#define AOT_SET_PC(pc) (gCPUState.CurrentPC = (u32)(pc))
#define AOT_SET_DELAY_EXEC() (gCPUState.Delay = EXEC_DELAY)
#define AOT_SET_DELAY_NONE() (gCPUState.Delay = NO_DELAY)
// A memory access can queue a Daedalus job *or* invalidate AOT code (a store
// to a code page, a PI DMA completion, a cache op).  In either case the
// generated block must stop at the same instruction boundary the interpreter
// would use.
extern "C" bool AOTRuntime_InvalidationPending();
#define AOT_HAS_PENDING_JOB()                                                  \
    (gCPUState.GetStuffToDo() != 0 || AOTRuntime_InvalidationPending())

#define AOT_READ8(address) Read8Bits(address)
#define AOT_READ16(address) Read16Bits(address)
#define AOT_READ32(address) Read32Bits(address)
#define AOT_READ64(address) Read64Bits(address)
#define AOT_WRITE8(address, value) Write8Bits((address), (value))
#define AOT_WRITE16(address, value) Write16Bits((address), (value))
#define AOT_WRITE32(address, value) Write32Bits((address), (value))
#define AOT_WRITE64(address, value) Write64Bits((address), (value))

#endif

enum AOTBlockStatus : u32
{
    AOT_STATUS_OK = 0,
    AOT_STATUS_STOP = 1,
};

struct AOTBlockResult
{
    u32 next_pc;
    u32 executed;
    u32 status;
};

struct AOTBlockDescriptor;
using AOTBlockFn = AOTBlockResult (*)();

struct AOTBlockDescriptor
{
    u32 start;
    // Offset of the first instruction in the normalized ROM.  The runtime does
    // not need this (it fingerprints resident RDRAM), but the host semantic
    // test uses it to load the exact bytes a block was proven from, including
    // when multiple mapping hypotheses describe the same virtual address.
    u32 source_offset;
    u32 instruction_count;
    u32 flags;
    u64 fingerprint;
    AOTBlockFn function;
};

// Per-block mutable state owned by the generated translation unit.  Keeping it
// there means the runtime needs no dynamic allocation and the table is sized
// exactly for the generated program.
struct AOTBlockRuntimeState
{
    u32 generation;
    u32 valid;
    u32 dispatch_count;
    u32 reserved;
};

// AOT_CHECK_JOB is used after every load/store.  A memory access can queue a
// TLB exception or another Daedalus job; the interpreter would finish the
// current instruction and then return to the scheduler, so the AOT block does
// the same.  The exception handler later restores the exact faulting PC.
#define AOT_CHECK_JOB(executed, next_pc)                                      \
    do                                                                        \
    {                                                                         \
        if (AOT_HAS_PENDING_JOB())                                            \
            return AOTBlockResult{(u32)(next_pc), (u32)(executed),            \
                                  AOT_STATUS_STOP};                           \
    } while (0)

static inline s32 AOT_SRA32(s32 value, u32 shift)
{
    if (shift == 0)
        return value;
    u32 result = (u32)value >> shift;
    if (value < 0)
        result |= (~0u << (32 - shift));
    return (s32)result;
}

static inline s64 AOT_SRA64(s64 value, u32 shift)
{
    if (shift == 0)
        return value;
    u64 result = (u64)value >> shift;
    if (value < 0)
        result |= (~0ull << (64 - shift));
    return (s64)result;
}

struct AOTDivResult
{
    u64 lo;
    u64 hi;
    bool changed;
};

static inline AOTDivResult AOT_DIV32(s32 dividend, s32 divisor)
{
    if (divisor == 0)
        return AOTDivResult{0, 0, false};
    // MIPS leaves the result unspecified for INT_MIN / -1.  Avoid the host
    // trap and use the value that the N64 hardware produces.
    if (dividend == (s32)0x80000000 && divisor == -1)
        return AOTDivResult{(u64)(s64)(s32)0x80000000, 0, true};
    const s32 quotient = dividend / divisor;
    const s32 remainder = dividend % divisor;
    return AOTDivResult{(u64)(s64)quotient, (u64)(s64)remainder, true};
}

static inline AOTDivResult AOT_DIVU32(u32 dividend, u32 divisor)
{
    if (divisor == 0)
        return AOTDivResult{0, 0, false};
    const u32 quotient = dividend / divisor;
    const u32 remainder = dividend % divisor;
    return AOTDivResult{(u64)(s64)(s32)quotient, (u64)(s64)(s32)remainder, true};
}

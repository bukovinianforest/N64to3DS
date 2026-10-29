#include "AOT/AOTRuntime.h"

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>

// Explicitly enable append-only AOT diagnostics in a dedicated build.  A
// normal release must not open the SD log from the CPU dispatch path.
#ifndef N64TO3DS_AOT_FILE_LOG
#define N64TO3DS_AOT_FILE_LOG 0
#endif
#if N64TO3DS_AOT_FILE_LOG != 0 && N64TO3DS_AOT_FILE_LOG != 1
#error N64TO3DS_AOT_FILE_LOG must be 0 or 1
#endif

#ifdef DAEDALUS_CTR
#include <3ds.h>
#endif

#include "Ultra/ultra_R4300.h"

// The generated translation unit owns these tables.  They must be declared at
// global scope: declarations inside the anonymous namespace below would bind
// to (anonymous namespace)::g_n64to3ds_aot_* instead of the generated symbols.
extern "C"
{
extern const AOTBlockDescriptor *const g_n64to3ds_aot_blocks;
extern const u32 g_n64to3ds_aot_block_count;
extern const u32 g_n64to3ds_aot_mapping_base;
extern const u32 g_n64to3ds_aot_mapped_size;
extern const char g_n64to3ds_aot_mapping_summary[];
extern const char g_n64to3ds_aot_rom_sha256[];
extern const char g_n64to3ds_aot_game_name[];
extern const char g_n64to3ds_aot_mode[];
extern AOTBlockRuntimeState *const g_n64to3ds_aot_state;
}

namespace
{
constexpr u64 kFnvOffset = 1469598103934665603ull;
constexpr u64 kFnvPrime = 1099511628211ull;
constexpr u32 kMaxFingerprintWords = 64;
#if N64TO3DS_AOT_FILE_LOG
constexpr const char * kLogPath = "sdmc:/3ds/DaedalusX64/n64to3ds-aot.log";
#endif

u64 ReadTick()
{
#ifdef DAEDALUS_CTR
    return svcGetSystemTick();
#else
    return 0;
#endif
}

s64 FreeApplicationMemory()
{
#ifdef DAEDALUS_CTR
    return static_cast<s64>(osGetMemRegionFree(MEMREGION_APPLICATION));
#else
    return 0;
#endif
}

// The generated translation unit owns these tables (declared at global scope
// above).

bool s_initialized = false;
bool s_invalidation_pending = false;
bool s_first_block_logged = false;
u32 s_generation = 1;
u32 s_fingerprint_logs = 0;
u64 s_dispatch_calls = 0;
u64 s_block_executions = 0;
u64 s_instructions_executed = 0;
u64 s_lookup_misses = 0;
u64 s_event_fallbacks = 0;
u64 s_fingerprint_failures = 0;
u64 s_stops = 0;
u64 s_invalid_results = 0;
u64 s_distinct_blocks = 0;
u64 s_timed_ticks = 0;
u64 s_timed_blocks = 0;
u64 s_total_frames = 0;
s64 s_min_free_memory = 0;
u32 s_heartbeat_calls = 0;
u64 s_last_heartbeat_tick = 0;
u64 s_last_heartbeat_dispatch = 0;
u32 s_new_block_logs = 0;

void LogLine(const char * format, ...)
{
#if N64TO3DS_AOT_FILE_LOG
    std::ofstream file(kLogPath, std::ios::out | std::ios::app);
    if (!file.is_open())
        return;

    char buffer[512];
    va_list args;
    va_start(args, format);
    std::vsnprintf(buffer, sizeof(buffer), format, args);
    va_end(args);
    file << buffer << '\n';
#else
    (void)format;
#endif
}

void LogStats(const char * label)
{
    LogLine(
        "stats[%s]: calls=%llu blocks=%llu distinct=%llu instructions=%llu "
        "misses=%llu event_fallbacks=%llu fingerprint_failures=%llu stops=%llu "
        "invalid_results=%llu generation=%u ram_free_min=%lld",
        label,
        static_cast<unsigned long long>(s_dispatch_calls),
        static_cast<unsigned long long>(s_block_executions),
        static_cast<unsigned long long>(s_distinct_blocks),
        static_cast<unsigned long long>(s_instructions_executed),
        static_cast<unsigned long long>(s_lookup_misses),
        static_cast<unsigned long long>(s_event_fallbacks),
        static_cast<unsigned long long>(s_fingerprint_failures),
        static_cast<unsigned long long>(s_stops),
        static_cast<unsigned long long>(s_invalid_results),
        s_generation,
        static_cast<long long>(s_min_free_memory));
}

u64 FingerprintWords(const u32 * words, u32 count)
{
    u64 hash = kFnvOffset;
    for (u32 index = 0; index < count; ++index)
    {
        const u32 word = words[index];
        for (int shift = 24; shift >= 0; shift -= 8)
        {
            hash ^= (word >> shift) & 0xFFu;
            hash *= kFnvPrime;
        }
    }
    return hash;
}

bool ReadResidentWords(u32 start, u32 count, u32 * words)
{
    if (count == 0 || count > kMaxFingerprintWords)
        return false;
    if ((start & 3u) != 0 || start < 0x80000000u)
        return false;

    const u64 end = static_cast<u64>(start) + static_cast<u64>(count) * 4ull;
    if (end > 0x80000000ull + static_cast<u64>(gRamSize))
        return false;

    // g_pu8RamBase_8000 is RDRAM - 0x80000000, so this addresses the exact
    // bytes the emulated CPU fetches.  The words are stored in the N64
    // little-endian word layout, i.e. native u32 reads give the big-endian
    // instruction value the analyzer hashed.
    const u8 * source = g_pu8RamBase_8000 + start;
    std::memcpy(words, source, static_cast<size_t>(count) * 4u);
    return true;
}

bool ValidateBlock(u32 index, const AOTBlockDescriptor & block)
{
    AOTBlockRuntimeState & state = g_n64to3ds_aot_state[index];
    if (state.generation == s_generation)
        return state.valid != 0;

    state.generation = s_generation;
    state.valid = 0;

    u32 words[kMaxFingerprintWords];
    if (!ReadResidentWords(block.start, block.instruction_count, words))
    {
        ++s_fingerprint_failures;
        if (s_fingerprint_logs < 32)
        {
            ++s_fingerprint_logs;
            LogLine(
                "fingerprint unavailable at 0x%08X (%u instructions); "
                "falling back to Daedalus",
                block.start,
                block.instruction_count);
        }
        return false;
    }

    const u64 actual = FingerprintWords(words, block.instruction_count);
    if (actual != block.fingerprint)
    {
        ++s_fingerprint_failures;
        if (s_fingerprint_logs < 64)
        {
            ++s_fingerprint_logs;
            LogLine(
                "fingerprint mismatch at 0x%08X (rom 0x%08X): expected %016llX, "
                "found %016llX; trying the next hypothesis or falling back to Daedalus",
                block.start,
                block.source_offset,
                static_cast<unsigned long long>(block.fingerprint),
                static_cast<unsigned long long>(actual));
        }
        return false;
    }

    state.valid = 1;
    return true;
}

u32 FindFirstBlock(u32 pc, bool & found)
{
    found = false;
    if (g_n64to3ds_aot_blocks == nullptr || g_n64to3ds_aot_block_count == 0)
        return 0;

    u32 low = 0;
    u32 high = g_n64to3ds_aot_block_count;
    while (low < high)
    {
        const u32 middle = low + (high - low) / 2;
        if (g_n64to3ds_aot_blocks[middle].start < pc)
            low = middle + 1;
        else
            high = middle;
    }

    if (low < g_n64to3ds_aot_block_count &&
        g_n64to3ds_aot_blocks[low].start == pc)
    {
        found = true;
    }
    return low;
}

void ResetStats()
{
    s_dispatch_calls = 0;
    s_block_executions = 0;
    s_instructions_executed = 0;
    s_lookup_misses = 0;
    s_event_fallbacks = 0;
    s_fingerprint_failures = 0;
    s_stops = 0;
    s_invalid_results = 0;
    s_distinct_blocks = 0;
    s_timed_ticks = 0;
    s_timed_blocks = 0;
    s_total_frames = 0;
    s_min_free_memory = 0;
    s_fingerprint_logs = 0;
    s_first_block_logged = false;
    s_heartbeat_calls = 0;
    s_last_heartbeat_tick = 0;
    s_last_heartbeat_dispatch = 0;
    s_new_block_logs = 0;
}
} // namespace

extern "C" bool AOTRuntime_InvalidationPending()
{
    return s_invalidation_pending;
}

namespace n64to3ds
{
void AOTRuntime_Initialize()
{
    try
    {
        std::filesystem::create_directories("sdmc:/3ds/DaedalusX64");
    }
    catch (...)
    {
    }

    ResetStats();
    s_initialized = true;
    s_invalidation_pending = false;
    ++s_generation;
    if (s_generation == 0)
        s_generation = 1;
    s_min_free_memory = FreeApplicationMemory();

    LogLine("n64to3ds AOT runtime");
    LogLine("mode=%s", g_n64to3ds_aot_mode);
    LogLine("game=%s", g_n64to3ds_aot_game_name);
    LogLine("rom_sha256=%s", g_n64to3ds_aot_rom_sha256);
    LogLine(
        "primary_mapping=0x%08X size=%u blocks=%u",
        g_n64to3ds_aot_mapping_base,
        g_n64to3ds_aot_mapped_size,
        g_n64to3ds_aot_block_count);
    LogLine("hypotheses=%s", g_n64to3ds_aot_mapping_summary);
    if (g_n64to3ds_aot_block_count == 0)
        LogLine("AOT disabled: no proven blocks; all execution uses Daedalus");
    else
        LogLine("AOT enabled: fingerprints are checked against resident RDRAM");
}

void AOTRuntime_Shutdown()
{
    if (!s_initialized)
        return;
    LogStats("shutdown");
    s_initialized = false;
}

bool AOTRuntime_IsEnabled()
{
    return s_initialized && g_n64to3ds_aot_blocks != nullptr &&
           g_n64to3ds_aot_block_count != 0;
}

bool AOTRuntime_TryExecute()
{
    if (!s_initialized)
        return false;

    ++s_dispatch_calls;

    if (!AOTRuntime_IsEnabled())
        return false;
    if (gCPUState.Delay != NO_DELAY)
        return false;
    if (gCPUState.GetStuffToDo() != 0)
        return false;

    // Consume an invalidation that happened before this dispatch.  The
    // generation was already bumped, so validation below sees fresh bytes.
    s_invalidation_pending = false;

    const u32 pc = gCPUState.CurrentPC;
    bool found = false;
    u32 index = FindFirstBlock(pc, found);
    if (!found)
    {
        ++s_lookup_misses;
        return false;
    }

    // Several mapping hypotheses can describe the same virtual address with
    // different ROM source bytes.  Try each descriptor until one matches the
    // bytes that are actually resident.  A mismatch is not an error: it simply
    // means that hypothesis was not the one this ROM's boot loader used.
    u32 candidate_end = index;
    while (candidate_end < g_n64to3ds_aot_block_count &&
           g_n64to3ds_aot_blocks[candidate_end].start == pc)
    {
        ++candidate_end;
    }

    for (; index < candidate_end; ++index)
    {
        const AOTBlockDescriptor & block = g_n64to3ds_aot_blocks[index];
        if (!ValidateBlock(index, block))
            continue;

        // Never execute a block across a scheduled event boundary.  The
        // interpreter updates CP0 Count once per instruction; the AOT block
        // does one bulk update, so the first event must be at least as far
        // away as the whole block.
        if (gCPUState.NumEvents == 0)
        {
            ++s_event_fallbacks;
            continue;
        }
        const s32 event_count = gCPUState.Events[0].mCount;
        if (event_count <= 0 || static_cast<u32>(event_count) < block.instruction_count)
        {
            ++s_event_fallbacks;
            continue;
        }

        gCPUState.CurrentPC = block.start;
        // Only the optional file log reports AOT time, so release builds skip
        // the system-tick reads.  Sampling keeps the diagnostic build cheap.
        const bool timed = N64TO3DS_AOT_FILE_LOG != 0 && (s_block_executions & 63ull) == 0ull;
        const u64 start_tick = timed ? ReadTick() : 0;
        const AOTBlockResult result = block.function();
        if (timed)
        {
            s_timed_ticks += ReadTick() - start_tick;
            ++s_timed_blocks;
        }

        AOTBlockRuntimeState & state = g_n64to3ds_aot_state[index];
        if (state.dispatch_count == 0)
        {
            ++s_distinct_blocks;
            if (s_new_block_logs < 64)
            {
                ++s_new_block_logs;
                LogLine(
                    "new_block=0x%08X rom=0x%08X instructions=%u",
                    block.start,
                    block.source_offset,
                    block.instruction_count);
            }
        }
        ++state.dispatch_count;

        if (result.executed == 0 || result.executed > block.instruction_count)
        {
            ++s_invalid_results;
            AOTRuntime_LogFatal("generated AOT block returned an invalid instruction count");
            gCPUState.CurrentPC = result.next_pc;
            gCPUState.Delay = NO_DELAY;
            return true;
        }

        ++s_block_executions;
        s_instructions_executed += result.executed;

        if (!s_first_block_logged)
        {
            s_first_block_logged = true;
            LogLine(
                "first_block_executed=0x%08X rom=0x%08X instructions=%u",
                block.start,
                block.source_offset,
                result.executed);
        }

        if (result.status == AOT_STATUS_OK)
        {
            gCPUState.CurrentPC = result.next_pc;
            gCPUState.Delay = NO_DELAY;
        }
        else
        {
            ++s_stops;
            const u32 jobs = gCPUState.GetStuffToDo();
            if ((jobs & CPU_CHECK_EXCEPTIONS) != 0)
            {
                // AOT_SET_PC() left CurrentPC on the faulting instruction and a
                // delay-slot access left Delay == EXEC_DELAY.  R4300_Handle_Exception
                // restores exactly those values.
            }
            else
            {
                gCPUState.CurrentPC = result.next_pc;
                gCPUState.Delay = NO_DELAY;
            }
        }

        CPU_UpdateCounter(result.executed);

        if ((s_block_executions % 100000ull) == 0ull)
            LogStats("progress");

        return true;
    }

    // Every descriptor at this PC either failed its fingerprint or could not
    // fit before the next scheduled event.  Let Daedalus execute the PC.
    return false;
}

void AOTRuntime_InvalidateAll()
{
    ++s_generation;
    if (s_generation == 0)
    {
        s_generation = 1;
        // Extremely unlikely; clear the cached validation so no block can be
        // considered valid under a recycled generation number.
        if (g_n64to3ds_aot_state != nullptr)
        {
            for (u32 i = 0; i < g_n64to3ds_aot_block_count; ++i)
                g_n64to3ds_aot_state[i].generation = 0;
        }
    }
    s_invalidation_pending = true;
}

void AOTRuntime_InvalidateRange(u32 address, u32 length)
{
    (void)address;
    (void)length;
    // A generation bump is intentionally conservative: a PI DMA or cache op
    // can cover many blocks, and re-fingerprinting a handful of blocks is
    // cheaper than maintaining an interval tree on the 3DS.
    AOTRuntime_InvalidateAll();
}

void AOTRuntime_LogFatal(const char * message)
{
#if N64TO3DS_AOT_FILE_LOG
    LogLine("FATAL: %s", message != nullptr ? message : "(null)");
#else
    // Preserve critical diagnostics in the main Daedalus log when optional
    // AOT file logging is disabled.
    std::cerr << "FATAL: " << (message != nullptr ? message : "(null)") << std::endl;
#endif
}

void AOTRuntime_MaybeLogHeartbeat(u32 pc)
{
    if (!s_initialized)
        return;

    // Sampling the tick on every dispatch is unnecessary and expensive.  A
    // few thousand calls between samples is still frequent enough to see a
    // stalled boot, while a long-running guest loop cannot hide for long.
    if ((++s_heartbeat_calls & 0xFFFu) != 0u)
        return;

#ifdef DAEDALUS_CTR
    const u64 now = svcGetSystemTick();
#else
    const u64 now = 0;
#endif
    bool due = false;
    if (now != 0)
    {
        if (s_last_heartbeat_tick == 0)
            s_last_heartbeat_tick = now;
        else if (now - s_last_heartbeat_tick >=
                 static_cast<u64>(SYSCLOCK_ARM11) * 15ull)
        {
            s_last_heartbeat_tick = now;
            due = true;
        }
    }

    // Emulators do not always implement svcGetSystemTick().  Fall back to a
    // dispatch-count interval so a heartbeat still proves whether the CPU loop
    // is advancing.  This is diagnostics-only and never affects execution.
    // When the tick works, do not use it: every log line is an SD write.
    if (now == 0)
    {
        const u64 dispatch = s_dispatch_calls;
        if (dispatch - s_last_heartbeat_dispatch >= 131072ull)
        {
            s_last_heartbeat_dispatch = dispatch;
            due = true;
        }
    }
    if (!due)
        return;

    LogLine(
        "heartbeat: pc=0x%08X delay=%u jobs=0x%08X events=%u event0=%d/%u "
        "count=0x%08X calls=%llu blocks=%llu distinct=%llu instructions=%llu "
        "misses=%llu event_fallbacks=%llu fingerprint_failures=%llu "
        "generation=%u",
        pc,
        static_cast<unsigned>(gCPUState.Delay),
        gCPUState.GetStuffToDo(),
        gCPUState.NumEvents,
        gCPUState.NumEvents != 0 ? gCPUState.Events[0].mCount : 0,
        gCPUState.NumEvents != 0
            ? static_cast<unsigned>(gCPUState.Events[0].mEventType)
            : 0u,
        gCPUState.CPUControl[C0_COUNT]._u32,
        static_cast<unsigned long long>(s_dispatch_calls),
        static_cast<unsigned long long>(s_block_executions),
        static_cast<unsigned long long>(s_distinct_blocks),
        static_cast<unsigned long long>(s_instructions_executed),
        static_cast<unsigned long long>(s_lookup_misses),
        static_cast<unsigned long long>(s_event_fallbacks),
        static_cast<unsigned long long>(s_fingerprint_failures),
        s_generation);
}

void AOTRuntime_LogFrame(float fps, float frame_ms, u32 last_pc, u32 frames)
{
#if !N64TO3DS_AOT_FILE_LOG
    // The statistics below only go to the optional AOT file log.
    (void)fps;
    (void)frame_ms;
    (void)last_pc;
    (void)frames;
#else
    if (!s_initialized || frames == 0)
        return;

    s_total_frames += frames;
    const s64 free_memory = FreeApplicationMemory();
    if (s_min_free_memory == 0 || (free_memory != 0 && free_memory < s_min_free_memory))
        s_min_free_memory = free_memory;

    double aot_ms_per_frame = 0.0;
    if (s_timed_blocks != 0)
    {
        const double ticks_per_block =
            static_cast<double>(s_timed_ticks) / static_cast<double>(s_timed_blocks);
        const double total_ticks =
            ticks_per_block * static_cast<double>(s_block_executions);
#ifdef DAEDALUS_CTR
        const double tick_hz = static_cast<double>(SYSCLOCK_ARM11);
#else
        const double tick_hz = 1.0;
#endif
        aot_ms_per_frame =
            (total_ticks * 1000.0 / tick_hz) / static_cast<double>(s_total_frames);
    }

    const double non_aot_ms =
        static_cast<double>(frame_ms) - aot_ms_per_frame;
    LogLine(
        "frame: fps=%.2f frame_ms=%.2f aot_ms_est=%.2f non_aot_ms_est=%.2f "
        "aot_hits=%llu aot_instructions=%llu misses=%llu event_fallbacks=%llu "
        "fingerprint_failures=%llu last_pc=0x%08X ram_free=%lld ram_free_min=%lld",
        static_cast<double>(fps),
        static_cast<double>(frame_ms),
        aot_ms_per_frame,
        non_aot_ms < 0.0 ? 0.0 : non_aot_ms,
        static_cast<unsigned long long>(s_block_executions),
        static_cast<unsigned long long>(s_instructions_executed),
        static_cast<unsigned long long>(s_lookup_misses),
        static_cast<unsigned long long>(s_event_fallbacks),
        static_cast<unsigned long long>(s_fingerprint_failures),
        last_pc,
        static_cast<long long>(free_memory),
        static_cast<long long>(s_min_free_memory));
#endif
}
} // namespace n64to3ds

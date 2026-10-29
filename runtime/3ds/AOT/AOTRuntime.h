// n64to3ds AOT runtime for the DaedalusX64 3DS core.
//
// The generated block table is the only interface between the host-side
// analyzer/code generator and the emulator.  The runtime validates each block
// against the bytes that are actually resident in emulated RDRAM before it is
// executed, and invalidates that validation whenever Daedalus reports a code
// cache/DMA write.

#pragma once

#include "AOTGeneratedCommon.h"

namespace n64to3ds
{
// Called after System_Open()/Memory_Reset() so the RDRAM base pointer and the
// embedded ROM are valid.  Safe to call more than once.
void AOTRuntime_Initialize();

// Writes the final statistics line.  Does not free anything (the generated
// tables are static).
void AOTRuntime_Shutdown();

// Execute one proven block at gCPUState.CurrentPC when the CPU is at an
// instruction boundary.  Returns true if a block ran (and therefore the
// caller must not execute a single interpreter instruction), false to fall
// back to the normal Daedalus core.
bool AOTRuntime_TryExecute();

// Called from CPU_InvalidateICache()/CPU_InvalidateICacheRange() and
// Memory_Reset().  Bumps a generation counter; every block is re-fingerprinted
// before its next execution.
void AOTRuntime_InvalidateAll();
void AOTRuntime_InvalidateRange(u32 address, u32 length);

bool AOTRuntime_IsEnabled();
void AOTRuntime_LogFatal(const char * message);

// Cheap wall-clock watchdog used while diagnosing a boot or a long-running
// guest loop.  File output requires N64TO3DS_AOT_FILE_LOG=1 at compile time;
// normal release builds do not write n64to3ds-aot.log.
void AOTRuntime_MaybeLogHeartbeat(u32 pc);

// Optional frame diagnostics.  fps/frame_ms are measured by the caller; the
// runtime adds its own AOT share-of-frame estimate, counters, last emulated PC
// and free/peak application memory when file logging is enabled.
void AOTRuntime_LogFrame(float fps, float frame_ms, u32 last_pc, u32 frames);
} // namespace n64to3ds

#pragma once

#include <cstddef>
#include <cstdint>

namespace n64to3ds
{
namespace dualscreen
{
// Access to emulated N64 RDRAM for dual-screen extensions: reads, plus the
// single-byte writes an extension may make from Extension::ApplyActions.
//
// Addresses are N64 virtual addresses in KSEG0 (0x80000000) or KSEG1
// (0xA0000000); they are translated to physical RDRAM and bounds-checked
// against the RDRAM size the emulator is currently using.  Values are
// returned in N64 (big-endian) order regardless of how Daedalus stores RAM.
// MMIO, TLB-mapped and out-of-range addresses are rejected, never followed.
//
// Consistency: the manager only calls extensions on the emulator thread at
// the end of an N64 frame, when no dynarec/AOT block and no PI DMA is in
// progress, so a group of reads in one Update() sees a single CPU state.
// The async audio thread writes only audio buffers in RDRAM.
class MemoryObserver
{
public:
    bool IsReadable(uint32_t address, size_t size) const;

    bool Read8(uint32_t address, uint8_t & value) const;
    bool Read16(uint32_t address, uint16_t & value) const;
    bool Read32(uint32_t address, uint32_t & value) const;
    bool Read64(uint32_t address, uint64_t & value) const;
    bool ReadF32(uint32_t address, float & value) const;

    // Copies `size` bytes in N64 byte order.  Fails without writing anything
    // if any byte of the range is not readable.
    bool ReadBuffer(uint32_t address, void * destination, size_t size) const;

    // Same address rules as the reads.  Only for guest *data* the game
    // itself rewrites the same way (no code, no DMA targets): Daedalus does
    // not invalidate translated code for writes made here.
    bool Write8(uint32_t address, uint8_t value);

private:
    bool Physical(uint32_t address, size_t size, uint32_t & physical) const;
};
}
}

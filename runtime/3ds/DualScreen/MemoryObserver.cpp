#include "DualScreen/MemoryObserver.h"

#include <cstring>

#include "Core/Memory.h"
#include "System/Endian.h"

namespace n64to3ds
{
namespace dualscreen
{
bool MemoryObserver::Physical(uint32_t address, size_t size, uint32_t & physical) const
{
    const uint32_t segment = address & 0xE0000000u;
    if (segment != 0x80000000u && segment != 0xA0000000u)
        return false;
    if (size == 0 || g_pu8RamBase == nullptr)
        return false;
    physical = address & 0x1FFFFFFFu;
    return static_cast<uint64_t>(physical) + size <= gRamSize;
}

bool MemoryObserver::IsReadable(uint32_t address, size_t size) const
{
    uint32_t physical = 0;
    return Physical(address, size, physical);
}

bool MemoryObserver::Read8(uint32_t address, uint8_t & value) const
{
    uint32_t physical = 0;
    if (!Physical(address, 1, physical))
        return false;
    value = g_pu8RamBase[physical ^ U8_TWIDDLE];
    return true;
}

bool MemoryObserver::Read16(uint32_t address, uint16_t & value) const
{
    uint32_t physical = 0;
    if ((address & 1) != 0 || !Physical(address, 2, physical))
        return false;
    value = *reinterpret_cast<const uint16_t *>(g_pu8RamBase + (physical ^ U16_TWIDDLE));
    return true;
}

bool MemoryObserver::Read32(uint32_t address, uint32_t & value) const
{
    uint32_t physical = 0;
    if ((address & 3) != 0 || !Physical(address, 4, physical))
        return false;
    // RDRAM words are stored in host order, so a native read is the
    // big-endian N64 value.
    value = *reinterpret_cast<const uint32_t *>(g_pu8RamBase + physical);
    return true;
}

bool MemoryObserver::Read64(uint32_t address, uint64_t & value) const
{
    uint32_t high = 0;
    uint32_t low = 0;
    if ((address & 7) != 0 || !Read32(address, high) || !Read32(address + 4, low))
        return false;
    value = (static_cast<uint64_t>(high) << 32) | low;
    return true;
}

bool MemoryObserver::ReadF32(uint32_t address, float & value) const
{
    uint32_t bits = 0;
    if (!Read32(address, bits))
        return false;
    std::memcpy(&value, &bits, sizeof(value));
    return true;
}

bool MemoryObserver::Write8(uint32_t address, uint8_t value)
{
    uint32_t physical = 0;
    if (!Physical(address, 1, physical))
        return false;
    g_pu8RamBase[physical ^ U8_TWIDDLE] = value;
    return true;
}

bool MemoryObserver::ReadBuffer(uint32_t address, void * destination, size_t size) const
{
    uint32_t physical = 0;
    if (destination == nullptr || !Physical(address, size, physical))
        return false;
    uint8_t * out = static_cast<uint8_t *>(destination);
    for (size_t i = 0; i < size; ++i)
        out[i] = g_pu8RamBase[(physical + i) ^ U8_TWIDDLE];
    return true;
}
}
}

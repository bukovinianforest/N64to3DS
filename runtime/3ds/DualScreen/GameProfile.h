#pragma once

#include <cstdint>
#include <map>
#include <string>

namespace n64to3ds
{
namespace dualscreen
{
// A verified game profile, written to RomFS by the host builder after it has
// matched the ROM's SHA-256 against tools/n64to3ds/profiles/*.json.
//
// Format: one `key=value` per line, `#` starts a comment.  Keys used by the
// manager: `id`, `extension`, `rom.crc1`, `rom.crc2`, `update_interval_frames`.
// Everything else (symbols, struct layout, name tables) belongs to the
// extension that the profile selects.
class GameProfile
{
public:
    bool Load(const char * path);

    const std::string * Find(const std::string & key) const;
    std::string GetString(const std::string & key, const std::string & fallback = std::string()) const;
    // Accepts decimal or 0x-prefixed hexadecimal values.
    bool GetU32(const std::string & key, uint32_t & value) const;

    const std::map<std::string, std::string> & Values() const { return mValues; }

private:
    std::map<std::string, std::string> mValues;
};
}
}

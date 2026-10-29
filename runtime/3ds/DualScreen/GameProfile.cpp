#include "DualScreen/GameProfile.h"

#include <cerrno>
#include <cstdlib>
#include <fstream>

namespace n64to3ds
{
namespace dualscreen
{
namespace
{
std::string Trim(const std::string & text)
{
    const size_t begin = text.find_first_not_of(" \t\r");
    if (begin == std::string::npos)
        return std::string();
    const size_t end = text.find_last_not_of(" \t\r");
    return text.substr(begin, end - begin + 1);
}
}

bool GameProfile::Load(const char * path)
{
    mValues.clear();
    std::ifstream file(path);
    if (!file.is_open())
        return false;

    std::string line;
    while (std::getline(file, line))
    {
        const size_t comment = line.find('#');
        if (comment != std::string::npos)
            line.erase(comment);
        const size_t equals = line.find('=');
        if (equals == std::string::npos)
            continue;
        const std::string key = Trim(line.substr(0, equals));
        if (!key.empty())
            mValues[key] = Trim(line.substr(equals + 1));
    }
    return !mValues.empty();
}

const std::string * GameProfile::Find(const std::string & key) const
{
    const auto it = mValues.find(key);
    return it == mValues.end() ? nullptr : &it->second;
}

std::string GameProfile::GetString(const std::string & key, const std::string & fallback) const
{
    const std::string * value = Find(key);
    return value != nullptr ? *value : fallback;
}

bool GameProfile::GetU32(const std::string & key, uint32_t & value) const
{
    const std::string * text = Find(key);
    if (text == nullptr || text->empty())
        return false;
    errno = 0;
    char * end = nullptr;
    const unsigned long parsed = std::strtoul(text->c_str(), &end, 0);
    if (errno != 0 || end == nullptr || *end != '\0' || parsed > 0xFFFFFFFFul)
        return false;
    value = static_cast<uint32_t>(parsed);
    return true;
}
}
}

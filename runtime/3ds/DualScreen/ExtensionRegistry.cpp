#include "DualScreen/Extension.h"

namespace n64to3ds
{
namespace dualscreen
{
std::unique_ptr<Extension> CreateHarvestMoon64Extension();

namespace
{
struct Registration
{
    const char * id;
    std::unique_ptr<Extension> (*create)();
};

// Keep in sync with tools/n64to3ds/extensions.py (EXTENSIONS).
const Registration kExtensions[] = {
    { "harvest_moon_64", CreateHarvestMoon64Extension },
};
}

std::unique_ptr<Extension> CreateExtension(const std::string & id)
{
    for (const Registration & registration : kExtensions)
    {
        if (id == registration.id)
            return registration.create();
    }
    return nullptr;
}
}
}

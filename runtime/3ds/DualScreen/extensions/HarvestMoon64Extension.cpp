// Harvest Moon 64 bottom-screen adapter.
//
// Everything game-specific that is a *fact about the ROM* (symbol addresses,
// Player struct offsets, item/tool names, game state constants) comes from
// the verified profile generated from hm64-decomp
// (tools/n64to3ds/profiles/harvest_moon_64.us.json).  This file only knows
// which of those facts to show and how.
//
// The Items tab redraws the game's pause screen (clock, money, tool,
// belongings and key item grids) from live state, so the inventory is
// visible while the game keeps running.  Its graphics are the game's own
// sprites, decoded from the user's ROM by the builder into
// romfs:/dualscreen/atlas.rgba; `sprite.<asset>.<animation>.<frame>` lines
// give each frame's atlas rectangle and origin, `icon.*` lines the animation
// that shows each tool/item.  Positions are the pause screen's view-space
// coordinates (src/game/overlayScreens.c/.h): origin at the screen centre,
// y up, one unit per pixel of the 320x240 N64 screen, which is exactly the
// size of the bottom screen.  Without the atlas the tab falls back to text.
//
// Slots are rearranged the way the pause screen does it: tap one slot, then
// another of the same kind, and the two are swapped (swapPauseScreenItems;
// the current tool and held item are slot 8 of their arrays there).  The
// swap is two byte writes into gPlayer, made at the end of an N64 frame and
// only while the game could open its pause screen.  Putting an item into or
// out of the player's hands is refused: the game must also create or remove
// the held object (initializePlayerHeldItem), which only its own code can do.

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <string>
#include <vector>

#include <GL/picaGL.h>
#include <imgui.h>

#include "DualScreen/Extension.h"

namespace n64to3ds
{
namespace dualscreen
{
namespace
{
constexpr int kToolSlots = 8;
constexpr int kBelongingSlots = 8;
constexpr int kKeyItemSlots = 24;
constexpr int kKeyItemsPerPage = 8;
constexpr int kToolLevelTools = 5;
// getToolLevelForAnimation: EMPTY_BOTTLE shows gPlayer.bottleContents.
constexpr uint8_t kEmptyBottle = 29;
// Bytes of Player read per update: up to and including `flags`.
constexpr uint32_t kPlayerReadSize = 0x78;
// A queued swap waits this many N64 frames for the player to be free.
constexpr uint32_t kSwapWaitFrames = 180;

// Pause-screen layout, view space (src/game/overlayScreens.h and
// loadPauseScreenSprites/loadClockSprites in overlayScreens.c).
constexpr float kGridBaseX = -74.0f;
constexpr float kGridSpacing = 35.0f;
constexpr float kToolRowY[2] = {33.0f, -2.0f};
constexpr float kBelongingRowY[2] = {-47.0f, -82.0f};
constexpr float kCurrentToolX = -28.0f;
constexpr float kHeldItemX = 20.0f;
constexpr float kSelectedRowY = 80.0f;
constexpr float kKeyItemsX = 77.0f;
constexpr float kKeyItemsSpacingX = 38.0f;
constexpr float kKeyItemsY = 31.0f;
constexpr float kKeyItemsSpacingY = 37.0f;
// Half the size of a grid cell, for touch targets.
constexpr float kCellHalf = 16.0f;

struct SpriteRect
{
    float x = 0, y = 0, w = 0, h = 0;	// atlas pixels
    float origin_x = 0, origin_y = 0;	// sprite origin relative to (x, y)
};

struct Field
{
    uint32_t offset = 0;
    uint32_t count = 0;
};

struct Snapshot
{
    bool valid = false;
    uint8_t tool_slots[kToolSlots] = {};
    uint8_t current_tool = 0;
    uint8_t tool_levels[kToolLevelTools] = {};
    uint8_t bottle_contents = 0;
    uint8_t belongings[kBelongingSlots] = {};
    uint8_t held_item = 0;
    uint8_t key_items[kKeyItemSlots] = {};
    uint8_t stamina = 0;
    uint8_t max_stamina = 0;
    uint32_t gold = 0;
    uint8_t season = 0;
    uint8_t day_of_month = 0;
    uint8_t day_of_week = 0;
    uint8_t year = 0;
    uint8_t hour = 0;
    uint8_t minutes = 0;
};

// One inventory slot.  Index 8 of tools/items is the current tool / held
// item; key item indices cover all three pages.
enum class SlotGroup { None, Tool, Item, KeyItem };
struct SlotRef
{
    SlotGroup group = SlotGroup::None;
    int index = 0;
    bool operator==(const SlotRef & other) const { return group == other.group && index == other.index; }
};
constexpr int kHandSlot = 8;

struct PendingSwap
{
    bool active = false;
    SlotRef a, b;
    uint8_t value_a = 0, value_b = 0;	// what the player saw; re-checked before writing
    uint32_t frames = 0;
};

class HarvestMoon64Extension final : public Extension
{
public:
    bool Initialise(const GameProfile & profile) override;
    void Update(const MemoryObserver & memory) override;
    bool ApplyActions(MemoryObserver & memory) override;
    int TabCount() const override { return 1; }
    const char * TabName(int) const override { return "Items"; }
    void DrawTab(int tab) override;
    bool FullScreen() const override { return mAtlasState != AtlasState::Failed; }
    void Shutdown() override;

private:
    bool LoadField(const GameProfile & profile, const char * name, const char * type,
                   uint32_t count, Field & field) const;
    void LoadNames(const GameProfile & profile, const std::string & prefix,
                   std::map<int, std::string> & names) const;
    std::string Name(const std::map<int, std::string> & names, uint8_t id) const;

    void DrawItems();
    bool DrawSlot(const char * id, const std::string & label, bool active, bool selected);

    // Pause-screen style drawing with the sprite atlas.
    enum class AtlasState { NotLoaded, Pending, Ready, Failed };
    bool LoadAtlas(const GameProfile & profile);
    bool UploadAtlas();
    const SpriteRect * FindSprite(const std::string & key) const;
    int IconAnimation(const std::string & category, uint8_t id, size_t variant) const;
    void DrawSprite(const SpriteRect & sprite, float view_x, float view_y, float angle_degrees = 0.0f,
                    float scale = 1.0f);
    void DrawSprite(const std::string & key, float view_x, float view_y, float angle_degrees = 0.0f,
                    float scale = 1.0f);
    void DrawNumber(const std::string & asset, uint32_t value, int max_digit, float view_x, float view_y,
                    float spacing);
    void DrawIcon(const std::string & category, uint8_t id, size_t variant, float view_x, float view_y);
    void SlotButton(const SlotRef & slot, float view_x, float view_y);
    uint8_t SlotValue(const SlotRef & slot) const;
    std::string SlotName(const SlotRef & slot) const;
    uint32_t SlotAddress(const SlotRef & slot) const;
    void TapSlot(const SlotRef & slot);
    bool PlayerIsFree(const MemoryObserver & memory) const;
    uint8_t ToolLevel(uint8_t tool) const;
    void DrawPauseScreen();

    uint32_t mPlayer = 0;
    uint32_t mGold = 0;
    uint32_t mSeason = 0;
    uint32_t mDayOfMonth = 0;
    uint32_t mDayOfWeek = 0;
    uint32_t mYear = 0;
    uint32_t mHour = 0;
    uint32_t mMinutes = 0;
    uint32_t mMaxStamina = 0;

    Field mStamina;
    Field mToolSlots;
    Field mCurrentTool;
    Field mBelongings;
    Field mHeldItem;
    Field mKeyItems;

    std::map<int, std::string> mToolNames;
    std::map<int, std::string> mItemNames;
    std::map<int, std::string> mKeyItemNames;
    std::map<int, std::string> mSeasonNames;
    std::map<int, std::string> mWeekdayNames;

    // Upgraded tools and the bottle show a different icon (level 0 without).
    bool mHasToolLevels = false;
    uint32_t mToolLevelsOffset = 0;
    uint32_t mBottleContentsOffset = 0;

    AtlasState mAtlasState = AtlasState::NotLoaded;
    std::vector<uint8_t> mAtlasPixels;	// freed after the upload
    uint32_t mAtlasWidth = 0;
    uint32_t mAtlasHeight = 0;
    GLuint mAtlasTexture = 0;
    std::map<std::string, SpriteRect> mSprites;
    std::map<std::string, std::string> mIconAssets;	// category -> sprite asset
    std::map<std::string, std::vector<int>> mIcons;	// "category.id" -> animation per variant
    int mKeyItemPage = 0;

    // Swapping slots: when the game could open its pause screen.
    bool mCanSwap = false;
    uint32_t mMainLoopIndex = 0;	// mainLoopCallbackCurrentIndex (u16)
    uint32_t mToolUseState = 0;	// toolUse.toolUseState (u8)
    uint32_t mActionHandlerOffset = 0;	// Player.actionHandler (u16)
    uint32_t mFlagsOffset = 0;	// Player.flags (u16)
    uint32_t mPlayerEntityFlags = 0;	// entities[ENTITY_PLAYER].flags (u16)
    uint32_t mEntityRequired = 0;	// active | currently animated
    uint32_t mEntityForbidden = 0;	// paused | map-space independent
    uint32_t mMainGame = 0;
    uint32_t mControllerInput = 0;
    uint32_t mRidingHorse = 0;

    SlotRef mPicked;	// first slot of a swap, group None when nothing is picked
    PendingSwap mPending;
    std::string mMessage;	// shown at the top when non-empty

    Snapshot mSnapshot;
    std::string mSelection;	// text fallback: full name of the tapped slot
};

// Player fields only the sprite UI needs.  Like LoadField, but a missing
// field only disables what depends on it.
bool LoadOptionalField(const GameProfile & profile, const char * name, const char * type, uint32_t count,
                       uint32_t & offset)
{
    const std::string prefix = std::string("layout.player.") + name;
    uint32_t profile_count = 0;
    const uint32_t size = std::strcmp(type, "u16") == 0 ? 2 : 1;
    return profile.GetU32(prefix + ".offset", offset) && profile.GetU32(prefix + ".count", profile_count) &&
           profile.GetString(prefix + ".type") == type && profile_count == count && offset % size == 0 &&
           offset + count * size <= kPlayerReadSize;
}

uint16_t BigEndian16(const uint8_t * bytes)
{
    return static_cast<uint16_t>((bytes[0] << 8) | bytes[1]);
}

bool HarvestMoon64Extension::LoadField(const GameProfile & profile, const char * name, const char * type,
                                       uint32_t count, Field & field) const
{
    const std::string prefix = std::string("layout.player.") + name;
    if (!profile.GetU32(prefix + ".offset", field.offset) || !profile.GetU32(prefix + ".count", field.count))
        return false;
    // The adapter decodes these fields with a fixed type; refuse a profile
    // that describes them differently instead of misreading memory.
    if (profile.GetString(prefix + ".type") != type || field.count != count)
        return false;
    const uint32_t size = std::strcmp(type, "f32") == 0 ? 4 : 1;
    return field.offset + field.count * size <= kPlayerReadSize;
}

void HarvestMoon64Extension::LoadNames(const GameProfile & profile, const std::string & prefix,
                                       std::map<int, std::string> & names) const
{
    for (const auto & entry : profile.Values())
    {
        if (entry.first.compare(0, prefix.size(), prefix) != 0)
            continue;
        names[std::atoi(entry.first.c_str() + prefix.size())] = entry.second;
    }
}

bool HarvestMoon64Extension::Initialise(const GameProfile & profile)
{
    const bool symbols = profile.GetU32("symbol.gPlayer", mPlayer) && profile.GetU32("symbol.gGold", mGold) &&
                         profile.GetU32("symbol.gSeason", mSeason) && profile.GetU32("symbol.gDayOfMonth", mDayOfMonth) &&
                         profile.GetU32("symbol.gDayOfWeek", mDayOfWeek) && profile.GetU32("symbol.gYear", mYear) &&
                         profile.GetU32("symbol.gHour", mHour) && profile.GetU32("symbol.gMinutes", mMinutes) &&
                         profile.GetU32("symbol.gMaximumStamina", mMaxStamina);
    const bool layout = LoadField(profile, "currentStamina", "u8", 1, mStamina) &&
                        LoadField(profile, "toolSlots", "u8", kToolSlots, mToolSlots) &&
                        LoadField(profile, "currentTool", "u8", 1, mCurrentTool) &&
                        LoadField(profile, "belongingsSlots", "u8", kBelongingSlots, mBelongings) &&
                        LoadField(profile, "heldItem", "u8", 1, mHeldItem) &&
                        LoadField(profile, "keyItemSlots", "u8", kKeyItemSlots, mKeyItems);
    if (!symbols || !layout)
        return false;

    mHasToolLevels = LoadOptionalField(profile, "toolLevels", "u8", kToolLevelTools, mToolLevelsOffset) &&
                     LoadOptionalField(profile, "bottleContents", "u8", 1, mBottleContentsOffset);
    uint32_t tool_use = 0;
    uint32_t tool_use_state_offset = 0;
    uint32_t entities = 0;
    uint32_t entity_flags_offset = 0;
    uint32_t active = 0, animated = 0, paused = 0, independent = 0;
    mCanSwap = profile.GetU32("symbol.entities", entities) &&
               profile.GetU32("inventory_swap.player_entity_flags_offset", entity_flags_offset) &&
               profile.GetU32("inventory_swap.entity_active", active) &&
               profile.GetU32("inventory_swap.entity_animated", animated) &&
               profile.GetU32("inventory_swap.entity_paused", paused) &&
               profile.GetU32("inventory_swap.entity_map_space_independent", independent) &&
               LoadOptionalField(profile, "actionHandler", "u16", 1, mActionHandlerOffset) &&
               LoadOptionalField(profile, "flags", "u16", 1, mFlagsOffset) &&
               profile.GetU32("symbol.mainLoopCallbackCurrentIndex", mMainLoopIndex) &&
               profile.GetU32("symbol.toolUse", tool_use) &&
               profile.GetU32("inventory_swap.tool_use_state_offset", tool_use_state_offset) &&
               profile.GetU32("inventory_swap.main_loop_main_game", mMainGame) &&
               profile.GetU32("inventory_swap.action_controller_input", mControllerInput) &&
               profile.GetU32("inventory_swap.flag_riding_horse", mRidingHorse) &&
               // The swap writes exactly these bytes; they must be where the
               // pause screen expects them: currentTool right after
               // toolSlots[7], heldItem right after belongingsSlots[7].
               mCurrentTool.offset == mToolSlots.offset + kToolSlots &&
               mHeldItem.offset == mBelongings.offset + kBelongingSlots;
    mToolUseState = tool_use + tool_use_state_offset;
    mPlayerEntityFlags = entities + entity_flags_offset;
    mEntityRequired = active | animated;
    mEntityForbidden = paused | independent;

    LoadNames(profile, "name.tool.", mToolNames);
    LoadNames(profile, "name.item.", mItemNames);
    LoadNames(profile, "name.key_item.", mKeyItemNames);
    LoadNames(profile, "name.season.", mSeasonNames);
    LoadNames(profile, "name.weekday.", mWeekdayNames);

    mAtlasState = LoadAtlas(profile) ? AtlasState::Pending : AtlasState::Failed;
    return true;
}

// Reads the atlas file and the sprite/icon tables.  The texture itself is
// created on the first draw, where GL may be used.
bool HarvestMoon64Extension::LoadAtlas(const GameProfile & profile)
{
    const std::string path = profile.GetString("atlas.file");
    if (path.empty() || !profile.GetU32("atlas.width", mAtlasWidth) || !profile.GetU32("atlas.height", mAtlasHeight) ||
        mAtlasWidth == 0 || mAtlasHeight == 0 || mAtlasWidth > 1024 || mAtlasHeight > 1024)
        return false;

    for (const auto & entry : profile.Values())
    {
        const std::string & key = entry.first;
        if (key.compare(0, 7, "sprite.") == 0)
        {
            int v[6];
            if (std::sscanf(entry.second.c_str(), "%d,%d,%d,%d,%d,%d", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6 ||
                v[0] < 0 || v[1] < 0 || v[2] <= 0 || v[3] <= 0 || uint32_t(v[0] + v[2]) > mAtlasWidth ||
                uint32_t(v[1] + v[3]) > mAtlasHeight)
                return false;
            SpriteRect & rect = mSprites[key.substr(7)];
            rect.x = v[0]; rect.y = v[1]; rect.w = v[2]; rect.h = v[3];
            rect.origin_x = v[4]; rect.origin_y = v[5];
        }
        else if (key.compare(0, 5, "icon.") == 0)
        {
            const size_t dot = key.find('.', 5);
            if (dot == std::string::npos)
                continue;
            const std::string category = key.substr(5, dot - 5);
            const std::string rest = key.substr(dot + 1);
            if (rest == "asset")
            {
                mIconAssets[category] = entry.second;
                continue;
            }
            std::vector<int> & animations = mIcons[category + "." + std::to_string(std::atoi(rest.c_str()))];
            const char * text = entry.second.c_str();
            while (*text != '\0')
            {
                char * end = nullptr;
                animations.push_back(static_cast<int>(std::strtol(text, &end, 10)));
                if (end == text)
                    return false;
                text = *end == ',' ? end + 1 : end;
            }
        }
    }
    if (mSprites.empty())
        return false;

    FILE * file = std::fopen(path.c_str(), "rb");
    if (file == nullptr)
        return false;
    mAtlasPixels.resize(size_t(mAtlasWidth) * mAtlasHeight * 4);
    const bool complete = std::fread(mAtlasPixels.data(), 1, mAtlasPixels.size(), file) == mAtlasPixels.size();
    std::fclose(file);
    if (!complete)
        std::vector<uint8_t>().swap(mAtlasPixels);
    return complete;
}

bool HarvestMoon64Extension::UploadAtlas()
{
    glGenTextures(1, &mAtlasTexture);
    if (mAtlasTexture == 0)
        return false;
    glBindTexture(GL_TEXTURE_2D, mAtlasTexture);
    // The game draws these sprites with bilinear filtering too; at 1:1 it
    // only matters for the rotated clock hands.  The builder pads every
    // sprite with transparent texels.
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, mAtlasWidth, mAtlasHeight, 0, GL_RGBA, GL_UNSIGNED_BYTE,
                 mAtlasPixels.data());
    std::vector<uint8_t>().swap(mAtlasPixels);
    return true;
}

void HarvestMoon64Extension::Shutdown()
{
    if (mAtlasTexture != 0)
        glDeleteTextures(1, &mAtlasTexture);
    mAtlasTexture = 0;
}

void HarvestMoon64Extension::Update(const MemoryObserver & memory)
{
    Snapshot next;
    uint8_t player[kPlayerReadSize];
    if (!memory.ReadBuffer(mPlayer, player, sizeof(player)))
    {
        mSnapshot.valid = false;
        return;
    }
    std::memcpy(next.tool_slots, player + mToolSlots.offset, kToolSlots);
    std::memcpy(next.belongings, player + mBelongings.offset, kBelongingSlots);
    std::memcpy(next.key_items, player + mKeyItems.offset, kKeyItemSlots);
    next.current_tool = player[mCurrentTool.offset];
    if (mHasToolLevels)
    {
        std::memcpy(next.tool_levels, player + mToolLevelsOffset, kToolLevelTools);
        next.bottle_contents = player[mBottleContentsOffset];
    }
    next.held_item = player[mHeldItem.offset];
    next.stamina = player[mStamina.offset];

    next.valid = memory.Read32(mGold, next.gold) && memory.Read8(mSeason, next.season) &&
                 memory.Read8(mDayOfMonth, next.day_of_month) && memory.Read8(mDayOfWeek, next.day_of_week) &&
                 memory.Read8(mYear, next.year) && memory.Read8(mHour, next.hour) &&
                 memory.Read8(mMinutes, next.minutes) && memory.Read8(mMaxStamina, next.max_stamina);
    mSnapshot = next;
}

// The conditions under which the player's Start button opens the pause
// screen: the main game loop updates the player (mainGameLoopCallback: the
// player entity is active, animated, map-space dependent, not paused), the
// player takes controller input (updatePlayerAction), no tool is in use and
// Jack is not on the horse (handlePlayerInput).  Cutscenes pause the player
// entity or move it out of map space.
bool HarvestMoon64Extension::PlayerIsFree(const MemoryObserver & memory) const
{
    uint16_t main_loop = 0;
    uint16_t entity_flags = 0;
    uint8_t tool_use_state = 0;
    uint8_t player[kPlayerReadSize];
    if (!memory.Read16(mMainLoopIndex, main_loop) || !memory.Read16(mPlayerEntityFlags, entity_flags) ||
        !memory.Read8(mToolUseState, tool_use_state) || !memory.ReadBuffer(mPlayer, player, sizeof(player)))
        return false;
    return main_loop == mMainGame && (entity_flags & mEntityRequired) == mEntityRequired &&
           (entity_flags & mEntityForbidden) == 0 && tool_use_state == 0 &&
           BigEndian16(player + mActionHandlerOffset) == mControllerInput &&
           (BigEndian16(player + mFlagsOffset) & mRidingHorse) == 0;
}

bool HarvestMoon64Extension::ApplyActions(MemoryObserver & memory)
{
    if (!mPending.active)
        return false;
    if (!PlayerIsFree(memory))
    {
        if (++mPending.frames > kSwapWaitFrames)
        {
            mPending.active = false;
            mMessage = "Busy - try again when you can move";
            std::cout << "n64to3ds dualscreen: swap timed out" << std::endl;
        }
        return false;
    }
    mPending.active = false;
    const uint32_t address_a = SlotAddress(mPending.a);
    const uint32_t address_b = SlotAddress(mPending.b);
    uint8_t value_a = 0;
    uint8_t value_b = 0;
    // Only swap what the player saw; anything else changed in the meantime.
    if (!memory.Read8(address_a, value_a) || !memory.Read8(address_b, value_b) || value_a != mPending.value_a ||
        value_b != mPending.value_b)
    {
        std::cout << "n64to3ds dualscreen: swap dropped, slots changed" << std::endl;
        return false;
    }
    memory.Write8(address_a, value_b);
    memory.Write8(address_b, value_a);
    return true;
}

std::string HarvestMoon64Extension::Name(const std::map<int, std::string> & names, uint8_t id) const
{
    const auto it = names.find(id);
    if (it != names.end())
        return it->second;
    char buffer[16];
    std::snprintf(buffer, sizeof(buffer), "#%u", id);	// not named in the decomp yet
    return buffer;
}

void HarvestMoon64Extension::DrawTab(int)
{
    if (mAtlasState == AtlasState::Pending)
        mAtlasState = UploadAtlas() ? AtlasState::Ready : AtlasState::Failed;
    if (mAtlasState == AtlasState::Ready)
    {
        // Full-screen mode: the background covers the whole screen, the
        // contents leave the bottom row to the manager's tab buttons.
        DrawSprite("background.0.0", 0.0f, 0.0f, 0.0f, 2.0f);	// initializePauseScreenBackground
        if (!mSnapshot.valid)
        {
            ImGui::SetCursorScreenPos(ImVec2(8, 8));
            ImGui::TextDisabled("Game state not available yet.");
        }
        else
            DrawPauseScreen();
        return;
    }
    if (!mSnapshot.valid)
    {
        ImGui::TextDisabled("Game state not available yet.");
        return;
    }
    DrawItems();
}

const SpriteRect * HarvestMoon64Extension::FindSprite(const std::string & key) const
{
    const auto it = mSprites.find(key);
    return it != mSprites.end() ? &it->second : nullptr;
}

// view space (origin at the screen centre, y up) -> bottom-screen pixels.
void HarvestMoon64Extension::DrawSprite(const SpriteRect & sprite, float view_x, float view_y, float angle_degrees,
                                        float scale)
{
    const ImVec2 origin(160.0f + view_x, 120.0f - view_y);
    const ImVec2 uv0(sprite.x / mAtlasWidth, sprite.y / mAtlasHeight);
    const ImVec2 uv1((sprite.x + sprite.w) / mAtlasWidth, (sprite.y + sprite.h) / mAtlasHeight);
    const float left = -sprite.origin_x * scale;
    const float top = -sprite.origin_y * scale;
    const float right = left + sprite.w * scale;
    const float bottom = top + sprite.h * scale;
    ImDrawList * draw = ImGui::GetWindowDrawList();
    const ImTextureID texture = reinterpret_cast<ImTextureID>(static_cast<intptr_t>(mAtlasTexture));
    if (angle_degrees == 0.0f)
    {
        draw->AddImage(texture, ImVec2(origin.x + left, origin.y + top), ImVec2(origin.x + right, origin.y + bottom),
                       uv0, uv1);
        return;
    }
    // setSpriteRotation turns counter-clockwise (y up); in screen space
    // (y down) the same visual rotation is x' = x c + y s, y' = -x s + y c.
    const float radians = angle_degrees * 3.14159265f / 180.0f;
    const float c = std::cos(radians);
    const float s = std::sin(radians);
    auto corner = [&](float x, float y) { return ImVec2(origin.x + x * c + y * s, origin.y - x * s + y * c); };
    draw->AddImageQuad(texture, corner(left, top), corner(right, top), corner(right, bottom), corner(left, bottom),
                       uv0, ImVec2(uv1.x, uv0.y), uv1, ImVec2(uv0.x, uv1.y));
}

void HarvestMoon64Extension::DrawSprite(const std::string & key, float view_x, float view_y, float angle_degrees,
                                        float scale)
{
    if (const SpriteRect * sprite = FindSprite(key))
        DrawSprite(*sprite, view_x, view_y, angle_degrees, scale);
}

// numberSprites.c: digit k sits at x - spacing * k, frame = digit; leading
// zeros are not drawn, a zero value still shows one digit.
void HarvestMoon64Extension::DrawNumber(const std::string & asset, uint32_t value, int max_digit, float view_x,
                                        float view_y, float spacing)
{
    uint32_t power = 1;
    for (int digit = 0; digit <= max_digit; ++digit, power *= 10)
    {
        if (digit != 0 && value / power == 0)
            break;
        DrawSprite(asset + "." + std::to_string(value / power % 10), view_x - spacing * digit, view_y);
    }
}

int HarvestMoon64Extension::IconAnimation(const std::string & category, uint8_t id, size_t variant) const
{
    const auto it = mIcons.find(category + "." + std::to_string(id));
    if (it == mIcons.end() || it->second.empty())
        return -1;
    return it->second[variant < it->second.size() ? variant : 0];
}

void HarvestMoon64Extension::DrawIcon(const std::string & category, uint8_t id, size_t variant, float view_x,
                                      float view_y)
{
    const auto asset = mIconAssets.find(category);
    const int animation = IconAnimation(category, id, variant);
    if (asset != mIconAssets.end() && animation >= 0)
        DrawSprite(asset->second + "." + std::to_string(animation) + ".0", view_x, view_y);
}

uint8_t HarvestMoon64Extension::SlotValue(const SlotRef & slot) const
{
    const Snapshot & s = mSnapshot;
    switch (slot.group)
    {
    case SlotGroup::Tool: return slot.index == kHandSlot ? s.current_tool : s.tool_slots[slot.index];
    case SlotGroup::Item: return slot.index == kHandSlot ? s.held_item : s.belongings[slot.index];
    case SlotGroup::KeyItem: return s.key_items[slot.index];
    default: return 0;
    }
}

std::string HarvestMoon64Extension::SlotName(const SlotRef & slot) const
{
    const uint8_t id = SlotValue(slot);
    if (id == 0)
        return std::string();
    switch (slot.group)
    {
    case SlotGroup::Tool: return Name(mToolNames, id);
    case SlotGroup::Item: return Name(mItemNames, id);
    default: return Name(mKeyItemNames, id);
    }
}

uint32_t HarvestMoon64Extension::SlotAddress(const SlotRef & slot) const
{
    switch (slot.group)
    {
    case SlotGroup::Tool: return mPlayer + mToolSlots.offset + slot.index;	// 8 = currentTool
    case SlotGroup::Item: return mPlayer + mBelongings.offset + slot.index;	// 8 = heldItem
    default: return mPlayer + mKeyItems.offset + slot.index;
    }
}

// Pause-screen selection: the first tap picks a slot (and names it), a tap
// on another slot of the same kind swaps the two, a tap on the picked slot
// drops the pick.
void HarvestMoon64Extension::TapSlot(const SlotRef & slot)
{
    mMessage.clear();
    if (mPicked.group == SlotGroup::None || mPicked.group != slot.group)
    {
        mPicked = SlotValue(slot) != 0 ? slot : SlotRef();
        mMessage = SlotName(slot);
        return;
    }
    const SlotRef first = mPicked;
    mPicked = SlotRef();
    if (first == slot)
        return;
    if (!mCanSwap || mPending.active)
    {
        mMessage = mCanSwap ? "Busy - try again when you can move" : "Swapping is not available";
        return;
    }
    if (slot.group == SlotGroup::Item && (first.index == kHandSlot || slot.index == kHandSlot))
    {
        mMessage = "Hold items via the game's pause menu";
        return;
    }
    mPending.active = true;
    mPending.a = first;
    mPending.b = slot;
    mPending.value_a = SlotValue(first);
    mPending.value_b = SlotValue(slot);
    mPending.frames = 0;
}

// Touch target over one grid cell.  A picked slot gets the pause screen's
// own selection frame (inventory animation 6, frame 1: the white one).
void HarvestMoon64Extension::SlotButton(const SlotRef & slot, float view_x, float view_y)
{
    char id[24];
    std::snprintf(id, sizeof(id), "##slot%d.%d", static_cast<int>(slot.group), slot.index);
    ImGui::SetCursorScreenPos(ImVec2(160.0f + view_x - kCellHalf, 120.0f - view_y - kCellHalf));
    if (ImGui::InvisibleButton(id, ImVec2(2 * kCellHalf, 2 * kCellHalf)))
        TapSlot(slot);
    if (mPicked == slot)
        DrawSprite("inventory.6.1", view_x, view_y);
}

uint8_t HarvestMoon64Extension::ToolLevel(uint8_t tool) const
{
    if (tool >= 1 && tool <= kToolLevelTools)
        return mSnapshot.tool_levels[tool - 1];
    return tool == kEmptyBottle ? mSnapshot.bottle_contents : 0;
}

void HarvestMoon64Extension::DrawPauseScreen()
{
    const Snapshot & s = mSnapshot;
    // gSeason is 1..4; item icons are listed per season.
    const size_t season = s.season >= 1 && s.season <= 4 ? s.season - 1 : 0;

    // Grids and labels (loadPauseScreenSprites).
    DrawSprite("inventory.0.0", 0.0f, 0.0f);
    DrawSprite("inventory.0.0", 0.0f, -80.0f);
    DrawSprite("inventory.1.0", 0.0f, 0.0f);
    DrawSprite("inventory.5.0", 0.0f, 0.0f);
    DrawSprite("inventory.2.0@3", kCurrentToolX, kSelectedRowY);
    DrawSprite("inventory.2.0@2", kHeldItemX, kSelectedRowY);

    // Clock (loadClockSprites).  The cow-skin face depends on a life event
    // flag the profile does not describe yet; the wooden one is the default.
    DrawSprite("clock.0.0", 0.0f, 0.0f);
    if (s.season >= 1 && s.season <= 4)
        DrawSprite("clock.1." + std::to_string(s.season - 1), 0.0f, 0.0f);
    if (s.day_of_week < 7)
        DrawSprite("clock.3." + std::to_string(s.day_of_week), 0.0f, 0.0f);
    DrawNumber("clock.2", s.day_of_month, 1, 0.0f, 0.0f, 8.0f);
    DrawSprite("clock.4.0", -79.0f, 81.0f, 360.0f - s.minutes * 6.0f);
    DrawSprite("clock.5.0", -80.0f, 80.0f, (360.0f - s.hour * 30.0f) - s.minutes * 0.5f);

    // Money (setMoneySprites): six digits, 10 units apart.
    DrawSprite("money.0.0", 0.0f, 0.0f);
    DrawNumber("money.1", s.gold > 999999 ? 999999 : s.gold, 5, 0.0f, 0.0f, 10.0f);

    // Icons (loadPauseScreenItemSprites).
    for (int i = 0; i < kToolSlots; ++i)
    {
        const float x = kGridBaseX + (i % 4) * kGridSpacing;
        const float y = kToolRowY[i / 4];
        const uint8_t tool = s.tool_slots[i];
        if (tool != 0)
            DrawIcon("tool", tool, ToolLevel(tool), x, y);
        SlotButton({SlotGroup::Tool, i}, x, y);
    }
    for (int i = 0; i < kBelongingSlots; ++i)
    {
        const float x = kGridBaseX + (i % 4) * kGridSpacing;
        const float y = kBelongingRowY[i / 4];
        const uint8_t item = s.belongings[i];
        if (item != 0)
            DrawIcon("item", item, season, x, y);
        SlotButton({SlotGroup::Item, i}, x, y);
    }
    if (s.current_tool != 0)
        DrawIcon("tool", s.current_tool, ToolLevel(s.current_tool), kCurrentToolX, kSelectedRowY);
    SlotButton({SlotGroup::Tool, kHandSlot}, kCurrentToolX, kSelectedRowY);
    if (s.held_item != 0)
        DrawIcon("item", s.held_item, season, kHeldItemX, kSelectedRowY);
    SlotButton({SlotGroup::Item, kHandSlot}, kHeldItemX, kSelectedRowY);

    const int pages = kKeyItemSlots / kKeyItemsPerPage;
    if (mKeyItemPage >= pages)
        mKeyItemPage = 0;
    for (int i = 0; i < kKeyItemsPerPage; ++i)
    {
        const float x = kKeyItemsX + (i & 1) * kKeyItemsSpacingX;
        const float y = kKeyItemsY - (i >> 1) * kKeyItemsSpacingY;
        const uint8_t key = s.key_items[mKeyItemPage * kKeyItemsPerPage + i];
        if (key != 0)
            DrawIcon("key_item", key, 0, x, y);
        SlotButton({SlotGroup::KeyItem, mKeyItemPage * kKeyItemsPerPage + i}, x, y);
    }

    // Bottom row, right half: key item pages (a picked key item stays
    // picked, so it can be swapped onto another page).
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6, 1));
    ImGui::SetCursorScreenPos(ImVec2(216, 240 - kNavBarHeight + 1));
    if (ImGui::ArrowButton("##keyprev", ImGuiDir_Left))
        mKeyItemPage = (mKeyItemPage + pages - 1) % pages;
    ImGui::SameLine(0, 4);
    ImGui::Text("%d/%d", mKeyItemPage + 1, pages);
    ImGui::SameLine(0, 4);
    if (ImGui::ArrowButton("##keynext", ImGuiDir_Right))
        mKeyItemPage = (mKeyItemPage + 1) % pages;
    ImGui::PopStyleVar();

    if (!mMessage.empty())
    {
        // Top edge, between the clock and the money, where it covers least.
        ImDrawList * draw = ImGui::GetWindowDrawList();
        const ImVec2 size = ImGui::CalcTextSize(mMessage.c_str());
        const ImVec2 min(160.0f - size.x / 2 - 6, 4.0f);
        const ImVec2 max(160.0f + size.x / 2 + 6, 4.0f + size.y + 4);
        draw->AddRectFilled(min, max, IM_COL32(30, 22, 12, 220), 4.0f);
        draw->AddText(ImVec2(min.x + 6, min.y + 2), IM_COL32(255, 245, 220, 255), mMessage.c_str());
    }
}

// One grid cell.  Long names are clipped to the cell; tapping a cell shows
// the full name below the grids.  Returns true when tapped.
bool HarvestMoon64Extension::DrawSlot(const char * id, const std::string & label, bool active, bool selected)
{
    const ImVec2 size(72, 28);
    const ImVec2 min = ImGui::GetCursorScreenPos();
    const bool pressed = ImGui::InvisibleButton(id, size);
    const ImVec2 max(min.x + size.x, min.y + size.y);
    ImDrawList * draw = ImGui::GetWindowDrawList();
    const ImU32 fill = label.empty() ? IM_COL32(40, 40, 46, 255) : IM_COL32(60, 64, 80, 255);
    draw->AddRectFilled(min, max, fill, 3.0f);
    if (active)
        draw->AddRect(min, max, IM_COL32(250, 200, 60, 255), 3.0f, ImDrawCornerFlags_All, 2.0f);
    else if (selected)
        draw->AddRect(min, max, IM_COL32(120, 180, 255, 255), 3.0f);
    if (!label.empty())
    {
        const ImVec4 clip(min.x + 3, min.y, max.x - 3, max.y);
        draw->AddText(ImGui::GetFont(), ImGui::GetFontSize(), ImVec2(min.x + 4, min.y + 6),
                      IM_COL32(235, 235, 240, 255), label.c_str(), nullptr, 0.0f, &clip);
    }
    return pressed;
}

void HarvestMoon64Extension::DrawItems()
{
    const Snapshot & s = mSnapshot;
    ImGui::Text("%uG", s.gold);
    ImGui::SameLine(70);
    ImGui::Text("%s %u %s, Y%u", Name(mSeasonNames, s.season).c_str(), s.day_of_month,
                Name(mWeekdayNames, s.day_of_week).c_str(), s.year);
    ImGui::SameLine(230);
    ImGui::Text("%02u:%02u", s.hour, s.minutes);

    ImGui::TextDisabled("Tools");
    for (int i = 0; i < kToolSlots; ++i)
    {
        if (i % 4 != 0)
            ImGui::SameLine();
        const uint8_t id = s.tool_slots[i];
        const std::string name = id != 0 ? Name(mToolNames, id) : std::string();
        char key[16];
        std::snprintf(key, sizeof(key), "##tool%d", i);
        if (DrawSlot(key, name, false, !name.empty() && mSelection == name) && !name.empty())
            mSelection = name;
    }

    ImGui::TextDisabled("Rucksack");
    for (int i = 0; i < kBelongingSlots; ++i)
    {
        if (i % 4 != 0)
            ImGui::SameLine();
        const uint8_t id = s.belongings[i];
        const std::string name = id != 0 ? Name(mItemNames, id) : std::string();
        char key[16];
        std::snprintf(key, sizeof(key), "##item%d", i);
        if (DrawSlot(key, name, false, !name.empty() && mSelection == name) && !name.empty())
            mSelection = name;
    }

    const std::string tool = s.current_tool != 0 ? Name(mToolNames, s.current_tool) : "-";
    const std::string held = s.held_item != 0 ? Name(mItemNames, s.held_item) : "-";
    ImGui::Text("Equipped: %s", tool.c_str());
    ImGui::Text("Holding: %s", held.c_str());
    if (s.max_stamina != 0)
        ImGui::Text("Stamina: %u / %u", s.stamina, s.max_stamina);

    std::string keys;
    for (int i = 0; i < kKeyItemSlots; ++i)
    {
        if (s.key_items[i] == 0)
            continue;
        if (!keys.empty())
            keys += ", ";
        keys += Name(mKeyItemNames, s.key_items[i]);
    }
    if (!keys.empty())
    {
        ImGui::TextDisabled("Key items");
        ImGui::TextWrapped("%s", keys.c_str());
    }

    if (!mSelection.empty())
    {
        ImGui::Separator();
        ImGui::TextWrapped("%s", mSelection.c_str());
    }
}
}

std::unique_ptr<Extension> CreateHarvestMoon64Extension()
{
    return std::unique_ptr<Extension>(new HarvestMoon64Extension());
}
}
}

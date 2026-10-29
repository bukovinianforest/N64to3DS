#include "DualScreen/DualScreen.h"

#include <cstdio>
#include <iostream>
#include <memory>

#include <3ds.h>
#include <GL/picaGL.h>

#include "Core/ROM.h"
#include "DualScreen/Extension.h"
#include "DualScreen/GameProfile.h"
#include "DualScreen/MemoryObserver.h"
#include "SysCTR/UI/UserInterface.h"

namespace n64to3ds
{
namespace
{
using dualscreen::Extension;
using dualscreen::GameProfile;
using dualscreen::MemoryObserver;

constexpr const char * kProfilePath = "romfs:/dualscreen/profile.txt";
// Used when the profile does not set update_interval_frames.  Inventory and
// position change at human speed; a few updates per second is plenty.
constexpr uint32_t kDefaultUpdateInterval = 6;

struct State
{
    GameProfile profile;
    MemoryObserver memory;
    std::unique_ptr<Extension> extension;
    uint32_t update_interval = kDefaultUpdateInterval;
    uint32_t frame = 0;
    int selected_tab = 0;
    bool show_emulator_menu = false;
    bool has_snapshot = false;
};

State * gState = nullptr;

void Log(const std::string & line)
{
    std::cout << "n64to3ds dualscreen: " << line << std::endl;
}

}

void DualScreen_Initialise()
{
    DualScreen_Shutdown();

    std::unique_ptr<State> state(new State());
    if (!state->profile.Load(kProfilePath))
        return;	// No profile embedded: stock emulator menu, nothing logged.

    // The builder matched the ROM's SHA-256 before embedding the profile.
    // Re-check the header CRCs so a profile can never be applied to a
    // different ROM (for example a hand-edited RomFS).
    // The profile stores the CRC words as they appear in the z64 header
    // (big-endian); Daedalus' RomID holds them byte-reversed (measured:
    // header 98DF9DFC -> CRC[0] FC9DDF98 for Harvest Moon 64).
    uint32_t crc1 = 0;
    uint32_t crc2 = 0;
    if (!state->profile.GetU32("rom.crc1", crc1) || !state->profile.GetU32("rom.crc2", crc2) ||
        __builtin_bswap32(crc1) != g_ROM.mRomID.CRC[0] || __builtin_bswap32(crc2) != g_ROM.mRomID.CRC[1])
    {
        char detail[96];
        std::snprintf(detail, sizeof(detail), " (profile %08X/%08X, ROM %08X/%08X)",
                      static_cast<unsigned>(crc1), static_cast<unsigned>(crc2),
                      static_cast<unsigned>(g_ROM.mRomID.CRC[0]), static_cast<unsigned>(g_ROM.mRomID.CRC[1]));
        Log("profile " + state->profile.GetString("id") + " does not match the running ROM; disabled" + detail);
        return;
    }

    const std::string extension_id = state->profile.GetString("extension");
    state->extension = dualscreen::CreateExtension(extension_id);
    if (!state->extension)
    {
        Log("extension '" + extension_id + "' is not compiled in; disabled");
        return;
    }
    if (!state->extension->Initialise(state->profile) || state->extension->TabCount() <= 0)
    {
        Log("extension '" + extension_id + "' rejected profile " + state->profile.GetString("id") + "; disabled");
        return;
    }

    uint32_t interval = 0;
    if (state->profile.GetU32("update_interval_frames", interval) && interval > 0)
        state->update_interval = interval;

    Log("active extension=" + extension_id + " profile=" + state->profile.GetString("id") +
        " update_interval_frames=" + std::to_string(state->update_interval));
    gState = state.release();
}

void DualScreen_Shutdown()
{
    if (gState == nullptr)
        return;
    if (gState->extension)
        gState->extension->Shutdown();
    delete gState;
    gState = nullptr;
}

bool DualScreen_IsActive()
{
    return gState != nullptr;
}

void DualScreen_ShowExtension()
{
    if (gState != nullptr)
        gState->show_emulator_menu = false;
}

void DualScreen_OnEndOfFrame()
{
    if (gState == nullptr)
        return;
    State & s = *gState;
    ++s.frame;

    if (s.extension->ApplyActions(s.memory))
        s.has_snapshot = false;
    if (!s.has_snapshot || s.frame % s.update_interval == 0)
    {
        s.extension->Update(s.memory);
        s.has_snapshot = true;
    }
}

namespace
{
// The extension draws the whole screen; the tab buttons and the way to the
// emulator menu sit in the bottom row.  They are placed explicitly, so even
// the first frame has them where they are drawn (see the Menu tab note
// below).
void DrawFullScreen(State & s)
{
    const int count = s.extension->TabCount();
    if (s.selected_tab >= count)
        s.selected_tab = 0;
    s.extension->DrawTab(s.selected_tab);

    ImGui::SetCursorScreenPos(ImVec2(2, 240 - dualscreen::kNavBarHeight + 1));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6, 1));
    // A single tab needs no button of its own.
    for (int tab = 0; count > 1 && tab < count; ++tab)
    {
        const bool selected = tab == s.selected_tab;
        if (selected)
            ImGui::PushStyleColor(ImGuiCol_Button, ImGui::GetStyleColorVec4(ImGuiCol_ButtonActive));
        if (ImGui::Button(s.extension->TabName(tab)))
            s.selected_tab = tab;
        if (selected)
            ImGui::PopStyleColor();
        ImGui::SameLine(0, 2);
    }
    if (ImGui::Button("Menu"))
        s.show_emulator_menu = true;
    ImGui::PopStyleVar();
}
}

bool DualScreen_DrawBottomScreen()
{
    if (gState == nullptr || gState->show_emulator_menu)
        return false;
    State & s = *gState;

    UI::RestoreRenderState();
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);

    ImGui_Impl3DS_NewFrame();
    const bool full_screen = s.extension->FullScreen();
    if (full_screen)
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::SetNextWindowPos(ImVec2(0, 0));
    ImGui::SetNextWindowSize(ImVec2(320, 240));
    ImGui::Begin("DualScreen", nullptr,
                 ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                 ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoBringToFrontOnFocus |
                 ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);

    if (full_screen)
    {
        ImGui::PopStyleVar();
        DrawFullScreen(s);
    }
    else if (ImGui::BeginTabBar("DualScreenTabs"))
    {
        const int count = s.extension->TabCount();
        for (int tab = 0; tab < count; ++tab)
        {
            if (ImGui::BeginTabItem(s.extension->TabName(tab)))
            {
                s.selected_tab = tab;
                ImGui::BeginChild("TabContents", ImVec2(0, 0), false);
                s.extension->DrawTab(tab);
                ImGui::EndChild();
                ImGui::EndTabItem();
            }
        }
        // The stock Daedalus menu (save states, options, close ROM) stays
        // reachable.  It is a normal tab with a deliberate button: a tab
        // *button* sits at (0,0) on the first frame, before ImGui has laid
        // out the bar, and a spurious touch there switched screens on boot.
        if (ImGui::BeginTabItem("Menu"))
        {
            ImGui::Spacing();
            ImGui::TextWrapped("Save states, options and Close ROM are in the emulator menu.");
            ImGui::Spacing();
            if (ImGui::Button("Open emulator menu", ImVec2(ImGui::GetContentRegionAvail().x, 48)))
                s.show_emulator_menu = true;
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }

    ImGui::End();
    ImGui::Render();
    ImGui_ImplOpenGL2_RenderDrawData(ImGui::GetDrawData());

    pglSelectScreen(GFX_TOP, GFX_LEFT);
    return true;
}
}

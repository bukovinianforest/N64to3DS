#pragma once

// Entry points the patched Daedalus calls.  All of them are no-ops when no
// verified game profile is embedded, so the stock bottom-screen menu keeps
// working for every ROM.

namespace n64to3ds
{
// After the ROM is open (g_ROM is valid).  Loads romfs:/dualscreen/profile.txt.
void DualScreen_Initialise();
void DualScreen_Shutdown();

// Emulator thread, end of every N64 frame.  Refreshes extension state at the
// profile's interval.
void DualScreen_OnEndOfFrame();

// Draws the extension UI on the bottom screen.  Returns false when the stock
// emulator menu should be drawn instead (no extension, or the user picked the
// emulator menu tab).
bool DualScreen_DrawBottomScreen();

// True while an extension is active; the stock menu then offers a way back.
bool DualScreen_IsActive();
void DualScreen_ShowExtension();
}

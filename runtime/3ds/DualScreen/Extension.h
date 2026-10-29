#pragma once

#include <memory>
#include <string>

#include "DualScreen/GameProfile.h"
#include "DualScreen/MemoryObserver.h"

namespace n64to3ds
{
namespace dualscreen
{
// Height of the tab-button row a full-screen extension leaves free at the
// bottom of the 320x240 screen.  The manager's buttons use its left half;
// the right half (x >= 160) belongs to the extension.
constexpr float kNavBarHeight = 18.0f;

// Interface every game-specific bottom-screen extension implements.
//
// Extensions observe guest state only through MemoryObserver, keep their own
// snapshot, and draw that snapshot with Dear ImGui on the bottom screen.  The
// only writes allowed are the ones an extension queues from DrawTab() and
// performs in ApplyActions(), after re-checking guest state there.  They do
// not depend on AOT or dynarec internals.
//
// Threading: every method runs on the emulator thread.  Update() is called
// between N64 frames at the profile's update interval; DrawTab() is called
// once per bottom-screen frame and must only use the snapshot.
class Extension
{
public:
    virtual ~Extension() = default;

    // Read the profile (addresses, layout, names).  Return false if anything
    // the extension needs is missing; the manager then falls back to the
    // stock emulator menu.
    virtual bool Initialise(const GameProfile & profile) = 0;

    // Refresh the snapshot from guest memory.  Must not block or scan RDRAM.
    virtual void Update(const MemoryObserver & memory) = 0;

    // End of an N64 frame, before Update(): perform actions the user queued
    // on the bottom screen.  Return true if guest memory was written, so the
    // snapshot is refreshed right away.
    virtual bool ApplyActions(MemoryObserver & memory) { (void)memory; return false; }

    virtual int TabCount() const = 0;
    virtual const char * TabName(int tab) const = 0;

    // Draw one tab's contents inside the manager's full-screen ImGui window
    // (320x240, below the tab bar).  Touch input arrives through ImGui.
    virtual void DrawTab(int tab) = 0;

    // When true, DrawTab() gets the whole screen instead: a window at (0,0)
    // without padding and no tab bar, with the bottom kNavBarHeight pixels
    // reserved for the manager's tab buttons.  Queried every frame.
    virtual bool FullScreen() const { return false; }

    virtual void Shutdown() {}
};

// Returns the compiled-in extension registered under `id`, or nullptr.
std::unique_ptr<Extension> CreateExtension(const std::string & id);
}
}

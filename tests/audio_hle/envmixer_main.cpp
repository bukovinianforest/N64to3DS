// Compares the upstream and n64to3ds ENVMIXER (ABI1, AudioHLEState::EnvMixer) on
// random DMEM, buffer layouts and saved ramp states; see tests/test_audio_hle.py.
#include "Base/Types.h"
#include "Core/Memory.h"
#include "HLEAudio/HLEAudioState.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <vector>

void * g_pMemoryBuffers[NUM_MEM_BUFFERS];
void RefEnvMixer(void * s, u8 flags, u32 address);
void NewEnvMixer(void * s, u8 flags, u32 address);

static std::mt19937 rng(12345);
static u32 R(u32 n) { return rng() % n; }
static s32 RS(s32 lo, s32 hi) { return lo + (s32)(rng() % (u32)(hi - lo + 1)); }

int main(int argc, char ** argv)
{
	const u32 iterations = argc > 1 ? atoi(argv[1]) : 200000;
	const u32 kRam = 1 << 20;
	std::vector<u8> ram_a(kRam), ram_b(kRam);
	AudioHLEState * a = static_cast<AudioHLEState *>(aligned_alloc(64, (sizeof(AudioHLEState) + 63) & ~63));
	AudioHLEState * b = static_cast<AudioHLEState *>(aligned_alloc(64, (sizeof(AudioHLEState) + 63) & ~63));
	u32 fast_cases = 0, mismatches = 0;
	for (u32 it = 0; it < iterations; ++it)
	{
		memset(a, 0, sizeof(*a));
		for (u32 i = 0; i < 0x1000; ++i) a->Buffer[i] = (u8)rng();
		// Mostly aligned, sometimes identical, overlapping or unaligned buffers.
		auto pick = [&]() -> u16 { u32 o = R(0xD00) & ~0xFu; if (R(20) == 0) o += 2; else if (R(20) == 0) o += 4; return (u16)o; };
		a->InBuffer = pick(); a->OutBuffer = pick(); a->AuxA = pick(); a->AuxC = pick(); a->AuxE = pick();
		if (R(4) == 0) a->OutBuffer = a->AuxA;
		if (R(6) == 0) a->InBuffer = a->OutBuffer;
		static const u16 kCounts[] = { 0x170, 0x170, 0x170, 0x10, 0x20, 0x160, 0x2, 0x18, 0x0, 0x1F0 };
		a->Count = kCounts[R(10)];
		a->VolLeft = (s16)RS(-32768, 32767); a->VolRight = (s16)RS(-32768, 32767);
		a->VolRampLeft = R(2) ? RS(0, 0x20000) : (s32)rng(); a->VolRampRight = R(2) ? RS(0, 0x20000) : (s32)rng();
		a->VolTrgLeft = (s16)RS(-32768, 32767); a->VolTrgRight = (s16)RS(-32768, 32767);
		a->EnvDry = (s16)RS(-32768, 32767); a->EnvWet = (s16)RS(-32768, 32767);
		const u32 address = (R(kRam - 64)) & ~7u;
		for (u32 i = 0; i < kRam; i += 4096) ram_a[i] = (u8)rng();
		{
			// Saved state: targets, ramps and adders, often settled.
			s16 * st = (s16 *)(ram_a.data() + address);
			st[0] = (s16)RS(-32768, 32767); st[2] = (s16)RS(-32768, 32767);
			s32 ltrg = (s32)(RS(-32768, 32767)) << 16, rtrg = (s32)(RS(-32768, 32767)) << 16;
			*(s32 *)(st + 4) = ltrg; *(s32 *)(st + 6) = rtrg;
			*(s32 *)(st + 8) = R(2) ? RS(0xF000, 0x11000) : (s32)rng(); *(s32 *)(st + 10) = R(2) ? RS(0xF000, 0x11000) : (s32)rng();
			auto adder = [&](s32 trg) { switch (R(4)) { case 0: return trg; case 1: return trg + RS(-0x100000, 0x100000); case 2: return (s32)rng(); default: return trg + RS(-8, 8); } };
			*(s32 *)(st + 12) = adder(ltrg); *(s32 *)(st + 14) = adder(rtrg);
			*(s32 *)(st + 16) = adder(ltrg); *(s32 *)(st + 18) = adder(rtrg);
		}
		const u8 flags = (u8)(R(2) ? 0 : 1) | (R(2) ? 8 : 0) | (R(4) == 0 ? 0x10 : 0);
		memcpy(b, a, sizeof(*a));
		memcpy(ram_b.data(), ram_a.data(), kRam);
		g_pMemoryBuffers[MEM_RD_RAM] = ram_a.data();
		RefEnvMixer(a, flags, address);
		g_pMemoryBuffers[MEM_RD_RAM] = ram_b.data();
		NewEnvMixer(b, flags, address);
		const bool aligned = !((a->InBuffer | a->OutBuffer | a->AuxA | a->AuxC | a->AuxE) & 3);
		fast_cases += aligned;
		if (memcmp(a, b, sizeof(*a)) != 0 || memcmp(ram_a.data() + address, ram_b.data() + address, 40) != 0)
		{
			if (++mismatches <= 5)
				printf("mismatch it=%u flags=%02x count=%x in=%x out=%x a=%x c=%x e=%x\n", it, flags, a->Count,
					   a->InBuffer, a->OutBuffer, a->AuxA, a->AuxC, a->AuxE);
		}
	}
	printf("envmixer: %u cases (%u with aligned buffers), %u mismatches\n", iterations, fast_cases, mismatches);
	return mismatches != 0;
}

// Compares the upstream and n64to3ds ADPCM decoders of the three ABIs
// (AudioHLEState::ADPCMDecode, ADPCM2, ADPCM3) on random DMEM, codebooks and
// commands; see tests/test_audio_hle.py.
#include "Base/Types.h"
#include "Core/Memory.h"
#include "HLEAudio/HLEAudioInternal.h"
#include "HLEAudio/HLEAudioState.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <vector>

void * g_pMemoryBuffers[NUM_MEM_BUFFERS];
AudioHLEState gAudioHLEState;
// Referenced by the other commands in ABI_ADPCM.cpp, not used here.
void AudioHLEState::ADPCMDecode(u8, u32) { abort(); }
void AudioHLEState::LoadADPCM(u32, u16) { abort(); }
void RefADPCMDecode(void * s, u8 flags, u32 address);
void NewADPCMDecode(void * s, u8 flags, u32 address);
namespace ref { void ADPCM2(AudioHLECommand); void ADPCM3(AudioHLECommand); }
namespace neu { void ADPCM2(AudioHLECommand); void ADPCM3(AudioHLECommand); }

static std::mt19937 rng(4242);
static u32 R(u32 n) { return rng() % n; }
static AudioHLECommand Cmd(u32 c0, u32 c1) { AudioHLECommand c; c._u64 = 0; c.cmd0 = c0; c.cmd1 = c1; return c; }

// Random DMEM, codebooks (small like real ones, or any 16-bit value) and state.
static void Randomise(AudioHLEState * s)
{
	for (u32 i = 0; i < 0x1000; ++i) s->Buffer[i] = (u8)rng();
	const bool wild = R(4) == 0;
	for (u16 & c : s->ADPCMTable) c = wild ? (u16)rng() : (u16)(s16)((s32)R(8192) - 4096);
	s->InBuffer = (u16)(R(0x600) & ~1u);
	s->OutBuffer = (u16)(0x600 + (R(0x400) & ~1u));
	static const u16 kCounts[] = { 0x170, 0x170, 0x20, 0x40, 0x0, 0x1f0, 0x10, 0x60 };
	s->Count = kCounts[R(8)];
	s->LoopVal = R(0x8000) & ~1u;
}

int main(int argc, char ** argv)
{
	const u32 iterations = argc > 1 ? atoi(argv[1]) : 50000;
	const u32 kRam = 1 << 16;
	std::vector<u8> ram(kRam), ram_ref(kRam);
	g_pMemoryBuffers[MEM_RD_RAM] = ram.data();
	// A codebook index from random data can point past ADPCMTable, and the
	// decoders then read whatever follows the state; give both copies the
	// same bytes there.
	const size_t kStateBytes = ((sizeof(AudioHLEState) + 63) & ~size_t(63)) + 1024;
	AudioHLEState * a = static_cast<AudioHLEState *>(aligned_alloc(64, kStateBytes));
	AudioHLEState * b = static_cast<AudioHLEState *>(aligned_alloc(64, kStateBytes));
	static AudioHLEState saved;
	u32 mismatches[3] = {};
	for (u32 it = 0; it < iterations; ++it)
	{
		for (u32 i = 0; i < kRam; i += 2) { ram[i] = (u8)rng(); ram[i + 1] = (u8)rng(); }
		const u32 address = R(kRam - 64) & ~1u;
		std::vector<u8> ram_start(ram);

		// ABI1
		memset(a, 0, kStateBytes);
		Randomise(a);
		memcpy(b, a, kStateBytes);
		const u8 flags = (u8)R(4);
		RefADPCMDecode(a, flags, address);
		ram_ref = ram;
		ram = ram_start;
		NewADPCMDecode(b, flags, address);
		if ((memcmp(a, b, sizeof(*a)) != 0 || ram != ram_ref) && ++mismatches[0] <= 3)
			printf("abi1 mismatch it=%u flags=%u count=%x\n", it, flags, a->Count);

		// ABI2 and ABI3 use the global state.
		for (u32 abi = 2; abi <= 3; ++abi)
		{
			ram = ram_start;
			Randomise(&gAudioHLEState);
			memcpy(&saved, &gAudioHLEState, sizeof(saved));
			const AudioHLECommand c = abi == 2
				? Cmd((R(8) << 16) | (u32)rng() & 0xffff, address)
				: Cmd(address, ((u32)R(8) << 28) | ((R(0x200) & ~1u) << 16) | (R(16) << 12) | (R(0x400) & ~1u));
			if (abi == 2) ref::ADPCM2(c); else ref::ADPCM3(c);
			ram_ref = ram;
			std::vector<u8> buf_ref(gAudioHLEState.Buffer, gAudioHLEState.Buffer + 0x10000);
			ram = ram_start;
			memcpy(&gAudioHLEState, &saved, sizeof(saved));
			if (abi == 2) neu::ADPCM2(c); else neu::ADPCM3(c);
			if ((memcmp(buf_ref.data(), gAudioHLEState.Buffer, 0x10000) != 0 || ram != ram_ref) && ++mismatches[abi - 1] <= 3)
				printf("abi%u mismatch it=%u cmd0=%08x cmd1=%08x\n", abi, it, c.cmd0, c.cmd1);
		}
	}
	const u32 total = mismatches[0] + mismatches[1] + mismatches[2];
	printf("adpcm: %u cases per ABI, mismatches abi1=%u abi2=%u abi3=%u, %u mismatches\n", iterations,
		   mismatches[0], mismatches[1], mismatches[2], total);
	return total != 0;
}

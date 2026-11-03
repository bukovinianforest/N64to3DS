// Compares the upstream and n64to3ds ENVMIXER2 (ABI2) and the ENVSETUP state it
// uses on random DMEM and commands; see tests/test_audio_hle.py.
#include "Base/Types.h"
#include "Core/Memory.h"
#include "HLEAudio/HLEAudioInternal.h"
#include "HLEAudio/HLEAudioState.h"
#include "HLEAudio/EnvMixerFast.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>

void * g_pMemoryBuffers[NUM_MEM_BUFFERS];
AudioHLEState gAudioHLEState;
void AudioHLEState::EnvMixer(u8, u32) { abort(); }
namespace ref { void ENVSETUP1(AudioHLECommand); void ENVSETUP2(AudioHLECommand); void ENVMIXER2(AudioHLECommand); extern bool isMKABI; }
namespace neu { void ENVSETUP1(AudioHLECommand); void ENVSETUP2(AudioHLECommand); void ENVMIXER2(AudioHLECommand); extern bool isMKABI; }

static std::mt19937 rng(777);
static AudioHLECommand Cmd(u32 c0, u32 c1) { AudioHLECommand c; c._u64 = 0; c.cmd0 = c0; c.cmd1 = c1; return c; }

int main(int argc, char ** argv)
{
	const u32 iterations = argc > 1 ? atoi(argv[1]) : 100000;
	static u8 before[0x10000], after_ref[0x10000];
	u32 mismatches = 0, fast = 0;
	for (u32 it = 0; it < iterations; ++it)
	{
		const bool mk = rng() & 1;
		ref::isMKABI = neu::isMKABI = mk;
		for (u32 i = 0; i < 0x1000; ++i) gAudioHLEState.Buffer[i] = (u8)rng();
		const AudioHLECommand s1 = Cmd(rng(), rng()), s2 = Cmd(rng(), rng());
		// Two mixes in a row: the envelope state carries over.
		AudioHLECommand mix[2];
		for (auto & m : mix)
		{
			u32 c0 = rng(), c1 = rng();
			if (rng() % 4 != 0)
			{
				// Distinct buffers, as real audio lists use.
				const u32 base[5] = { 0x000, 0x200, 0x400, 0x600, 0x800 };
				u32 o[5]; for (u32 i = 0; i < 5; ++i) o[i] = base[i] + (rng() % 0x10) * 0x10;
				for (u32 i = 0; i < 5; ++i) { u32 j = rng() % 5; std::swap(o[i], o[j]); }
				c0 = (c0 & ~(0xff0u << 12)) | (o[0] << 12);
				c1 = (o[1] << 20) | (o[2] << 12) | (o[3] << 4) | (o[4] >> 4);
				c0 = (c0 & ~0xff00u) | ((rng() % 0xb9) << 8);
			}
			m = Cmd(c0, c1);
		}
		memcpy(before, gAudioHLEState.Buffer, sizeof(before));
		ref::ENVSETUP1(s1); ref::ENVSETUP2(s2); ref::ENVMIXER2(mix[0]); ref::ENVMIXER2(mix[1]);
		memcpy(after_ref, gAudioHLEState.Buffer, sizeof(after_ref));
		memcpy(gAudioHLEState.Buffer, before, sizeof(before));
		neu::ENVSETUP1(s1); neu::ENVSETUP2(s2); neu::ENVMIXER2(mix[0]); neu::ENVMIXER2(mix[1]);
		{
			const AudioHLECommand & m = mix[0];
			const u32 o[5] = { (m.cmd0 >> 12) & 0xff0, (m.cmd1 >> 20) & 0xff0, (m.cmd1 >> 12) & 0xff0, (m.cmd1 >> 4) & 0xff0, (m.cmd1 << 4) & 0xff0 };
			const s32 count = (m.cmd0 >> 8) & 0xff;
			const u32 adder = mk ? 8 : 16;
			fast += count > 0 && n64to3ds_audio::EnvMixer2FastApplies(o, 5, ((count + adder - 1) / adder) * adder * 2);
		}
		if (memcmp(after_ref, gAudioHLEState.Buffer, sizeof(after_ref)) != 0 && ++mismatches <= 5)
			printf("mismatch it=%u mk=%d cmd0=%08x cmd1=%08x\n", it, mk, mix[0].cmd0, mix[0].cmd1);
	}
	printf("envmixer2: %u cases (%u on the fast path), %u mismatches\n", iterations, fast, mismatches);
	return mismatches != 0;
}

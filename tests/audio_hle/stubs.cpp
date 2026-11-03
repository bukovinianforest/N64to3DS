// Symbols the audio HLE sources reference but the commands under test do not use.
#include <cstddef>
#include <cstdlib>
void memcpy_byteswap(void *, const void *, size_t) { abort(); }	// not used by the commands under test

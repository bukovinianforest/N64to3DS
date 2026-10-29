// n64to3ds console agent.
//
// A small Wi-Fi service for the Old 3DS so that a host can copy files,
// install n64to3ds CIAs and launch them without FBI or ftpd.  The protocol is
// line based on TCP port 5001 (see tools/console-agent/README.md).
//
// Safety rules, enforced here and not only on the host:
//  * files: only below the roots in kAllowedRoots, no "..";
//  * titles: only SD applications (0x00040000xxxxxx00) whose installed
//    product code is the one n64to3ds generates (CTR-P-<low 16 bits of the
//    unique id>).  A CIA whose title is not installed yet may be installed;
//    any other installed title is refused, so retail games and system titles
//    can never be replaced, deleted or launched by the agent.

#include <3ds.h>

#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <malloc.h>
#include <netinet/in.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#define AGENT_PORT        5001
#define AGENT_VERSION     "4"
#define AGENT_TITLE_ID    0x000400000A6E7000ULL
#define SOC_BUFFER_SIZE   0x100000
#define IO_CHUNK          (128 * 1024)
#define LINE_MAX_LEN      512
#define PATH_MAX_LEN      256

static const char *kAllowedRoots[] = {
	"/cias/",
	"/3ds/DaedalusX64/",
	"/3ds/n64to3ds-agent/",
};

// Read-only roots: LS and GET only (Luma crash dumps).
static const char *kReadOnlyRoots[] = {
	"/luma/dumps/",
};

static bool sQuit = false;
static u64 sLaunchTitle = 0;

// ---------------------------------------------------------------------------
// Connection with a small read buffer, so that a command line and the payload
// that follows it can be read from the same stream.

typedef struct
{
	int fd;
	char buf[16 * 1024];
	size_t len;
	size_t pos;
} Conn;

static bool SendAll(Conn *c, const void *data, size_t size)
{
	const char *p = (const char *)data;
	while (size > 0)
	{
		ssize_t n = send(c->fd, p, size, 0);
		if (n <= 0)
			return false;
		p += n;
		size -= (size_t)n;
	}
	return true;
}

static bool Reply(Conn *c, const char *fmt, ...)
{
	char line[LINE_MAX_LEN];
	va_list ap;
	va_start(ap, fmt);
	int n = vsnprintf(line, sizeof(line) - 1, fmt, ap);
	va_end(ap);
	if (n < 0)
		return false;
	if ((size_t)n > sizeof(line) - 2)
		n = sizeof(line) - 2;
	line[n++] = '\n';
	return SendAll(c, line, (size_t)n);
}

static bool Fill(Conn *c)
{
	ssize_t n = recv(c->fd, c->buf, sizeof(c->buf), 0);
	if (n <= 0)
		return false;
	c->len = (size_t)n;
	c->pos = 0;
	return true;
}

// Reads up to `size` bytes; returns the number read, 0 when the peer closed.
static size_t ReadSome(Conn *c, void *dst, size_t size)
{
	// Large reads go straight from the socket into the caller's buffer.
	if (c->pos == c->len && size >= sizeof(c->buf))
	{
		ssize_t n = recv(c->fd, dst, size, 0);
		return n > 0 ? (size_t)n : 0;
	}
	if (c->pos == c->len && !Fill(c))
		return 0;
	size_t n = c->len - c->pos;
	if (n > size)
		n = size;
	memcpy(dst, c->buf + c->pos, n);
	c->pos += n;
	return n;
}

static bool ReadLine(Conn *c, char *line, size_t size)
{
	size_t used = 0;
	for (;;)
	{
		if (c->pos == c->len && !Fill(c))
			return false;
		char ch = c->buf[c->pos++];
		if (ch == '\n')
			break;
		if (ch == '\r')
			continue;
		if (used + 1 >= size)
			return false;
		line[used++] = ch;
	}
	line[used] = '\0';
	return true;
}

// ---------------------------------------------------------------------------
// Paths and titles

static bool PathSyntaxOk(const char *path)
{
	size_t len = strlen(path);
	if (len == 0 || len >= PATH_MAX_LEN || strstr(path, "..") != NULL || strchr(path, '\\') != NULL)
		return false;
	for (size_t i = 0; i < len; ++i)
		if ((unsigned char)path[i] < 0x20 || (unsigned char)path[i] > 0x7E)
			return false;
	return true;
}

static bool PathUnderRoots(const char *path, const char *const *roots, size_t count)
{
	const size_t len = strlen(path);
	for (size_t i = 0; i < count; ++i)
	{
		const size_t root = strlen(roots[i]);
		// The root directory itself (without the trailing slash) is allowed too.
		if (strncmp(path, roots[i], root) == 0 || (len == root - 1 && strncmp(path, roots[i], len) == 0))
			return true;
	}
	return false;
}

static bool PathWritable(const char *path)
{
	return PathSyntaxOk(path) && PathUnderRoots(path, kAllowedRoots, sizeof(kAllowedRoots) / sizeof(kAllowedRoots[0]));
}

static bool PathReadable(const char *path)
{
	return PathWritable(path)
		|| (PathSyntaxOk(path) && PathUnderRoots(path, kReadOnlyRoots, sizeof(kReadOnlyRoots) / sizeof(kReadOnlyRoots[0])));
}

static void SdmcPath(char *out, size_t size, const char *path)
{
	snprintf(out, size, "sdmc:%s", path);
}

typedef enum
{
	TITLE_REFUSED,
	TITLE_NOT_INSTALLED,
	TITLE_OURS_INSTALLED,
} TitleState;

static TitleState CheckTitle(u64 title_id, const char **why)
{
	if ((title_id >> 32) != 0x00040000 || (title_id & 0xFF) != 0)
	{
		*why = "not an SD application title id";
		return TITLE_REFUSED;
	}
	if (title_id == AGENT_TITLE_ID)
	{
		*why = "the agent can only update itself with INSTALL";
		return TITLE_REFUSED;
	}
	char code[16 + 1];
	memset(code, 0, sizeof(code));
	if (R_FAILED(AM_GetTitleProductCode(MEDIATYPE_SD, title_id, code)))
		return TITLE_NOT_INSTALLED;
	char expected[16];
	snprintf(expected, sizeof(expected), "CTR-P-%04lX", (unsigned long)((title_id >> 8) & 0xFFFF));
	if (strcmp(code, expected) != 0)
	{
		*why = "installed title was not made by n64to3ds (product code mismatch)";
		return TITLE_REFUSED;
	}
	return TITLE_OURS_INSTALLED;
}

// ---------------------------------------------------------------------------
// Commands

// Returns false when the connection must be dropped (payload not consumed).
static bool CmdPut(Conn *c, const char *path, unsigned long long size)
{
	char target[PATH_MAX_LEN + 8], part[PATH_MAX_LEN + 16];
	SdmcPath(target, sizeof(target), path);
	snprintf(part, sizeof(part), "%s.part", target);
	FILE *f = fopen(part, "wb");
	if (f == NULL)
	{
		Reply(c, "ERR cannot create %s (errno %d)", path, errno);
		return false;
	}
	char *buf = (char *)malloc(IO_CHUNK);
	unsigned long long left = size, next_report = 0;
	bool ok = buf != NULL;
	while (ok && left > 0)
	{
		// Write whole chunks: many small SD writes made uploads several
		// times slower than the network.
		size_t want = left < IO_CHUNK ? (size_t)left : IO_CHUNK;
		size_t got = 0;
		while (got < want)
		{
			size_t n = ReadSome(c, buf + got, want - got);
			if (n == 0)
				break;
			got += n;
		}
		if (got != want || fwrite(buf, 1, got, f) != got)
			ok = false;
		left -= got;
		if (size - left >= next_report)
		{
			printf("\x1b[2K\r  put %llu/%llu KiB", (size - left) / 1024, size / 1024);
			next_report += 1024 * 1024;
		}
	}
	printf("\n");
	free(buf);
	if (fclose(f) != 0)
		ok = false;
	if (!ok)
	{
		remove(part);
		Reply(c, "ERR transfer of %s failed after %llu bytes", path, size - left);
		return false;
	}
	remove(target);
	if (rename(part, target) != 0)
	{
		Reply(c, "ERR cannot rename %s.part (errno %d)", path, errno);
		return true;
	}
	return Reply(c, "OK");
}

static void CmdGet(Conn *c, const char *path)
{
	char full[PATH_MAX_LEN + 8];
	SdmcPath(full, sizeof(full), path);
	FILE *f = fopen(full, "rb");
	struct stat st;
	if (f == NULL || fstat(fileno(f), &st) != 0)
	{
		if (f != NULL)
			fclose(f);
		Reply(c, "ERR cannot open %s", path);
		return;
	}
	Reply(c, "OK %lld", (long long)st.st_size);
	char *buf = (char *)malloc(IO_CHUNK);
	size_t n;
	while (buf != NULL && (n = fread(buf, 1, IO_CHUNK, f)) > 0)
		if (!SendAll(c, buf, n))
			break;
	free(buf);
	fclose(f);
}

static void CmdList(Conn *c, const char *path)
{
	char full[PATH_MAX_LEN + 8];
	SdmcPath(full, sizeof(full), path);
	DIR *d = opendir(full);
	if (d == NULL)
	{
		Reply(c, "ERR cannot open directory %s", path);
		return;
	}
	Reply(c, "OK");
	struct dirent *e;
	while ((e = readdir(d)) != NULL)
	{
		char child[PATH_MAX_LEN * 2 + 8];
		snprintf(child, sizeof(child), "%s/%s", full, e->d_name);
		struct stat st;
		if (stat(child, &st) != 0)
			continue;
		Reply(c, "%c %lld %s", S_ISDIR(st.st_mode) ? 'D' : 'F', (long long)st.st_size, e->d_name);
	}
	closedir(d);
	Reply(c, ".");
}

static void CmdDelete(Conn *c, const char *path)
{
	char full[PATH_MAX_LEN + 8];
	SdmcPath(full, sizeof(full), path);
	if (remove(full) != 0)
		Reply(c, "ERR cannot delete %s (errno %d)", path, errno);
	else
		Reply(c, "OK");
}

static void CmdTitles(Conn *c)
{
	u32 count = 0;
	if (R_FAILED(AM_GetTitleCount(MEDIATYPE_SD, &count)))
	{
		Reply(c, "ERR AM_GetTitleCount failed");
		return;
	}
	u64 *ids = (u64 *)calloc(count ? count : 1, sizeof(u64));
	u32 read = 0;
	if (ids == NULL || R_FAILED(AM_GetTitleList(&read, MEDIATYPE_SD, count, ids)))
	{
		free(ids);
		Reply(c, "ERR AM_GetTitleList failed");
		return;
	}
	Reply(c, "OK");
	for (u32 i = 0; i < read; ++i)
	{
		const char *why = "";
		if (CheckTitle(ids[i], &why) == TITLE_OURS_INSTALLED)
			Reply(c, "%016llX", (unsigned long long)ids[i]);
	}
	free(ids);
	Reply(c, ".");
}

static Result InstallFromFile(Handle file, u64 size)
{
	Handle cia;
	Result res = AM_StartCiaInstall(MEDIATYPE_SD, &cia);
	if (R_FAILED(res))
		return res;
	char *buf = (char *)malloc(IO_CHUNK);
	if (buf == NULL)
	{
		AM_CancelCIAInstall(cia);
		return -1;
	}
	u64 offset = 0, next_report = 0;
	while (R_SUCCEEDED(res) && offset < size)
	{
		u32 got = 0, written = 0;
		res = FSFILE_Read(file, &got, offset, buf, IO_CHUNK);
		if (R_SUCCEEDED(res) && got == 0)
			res = -2;
		if (R_SUCCEEDED(res))
			res = FSFILE_Write(cia, &written, offset, buf, got, 0);
		offset += got;
		if (offset >= next_report)
		{
			printf("\x1b[2K\r  install %llu/%llu KiB", offset / 1024, size / 1024);
			next_report += 1024 * 1024;
		}
	}
	printf("\n");
	free(buf);
	if (R_FAILED(res))
	{
		AM_CancelCIAInstall(cia);
		return res;
	}
	return AM_FinishCiaInstall(cia);
}

static void CmdInstall(Conn *c, const char *path)
{
	Handle file;
	Result res = FSUSER_OpenFileDirectly(&file, ARCHIVE_SDMC, fsMakePath(PATH_EMPTY, ""),
		fsMakePath(PATH_ASCII, path), FS_OPEN_READ, 0);
	if (R_FAILED(res))
	{
		Reply(c, "ERR cannot open %s (0x%08lX)", path, (unsigned long)res);
		return;
	}
	AM_TitleEntry entry;
	u64 size = 0;
	res = AM_GetCiaFileInfo(MEDIATYPE_SD, &entry, file);
	if (R_SUCCEEDED(res))
		res = FSFILE_GetSize(file, &size);
	if (R_FAILED(res))
	{
		FSFILE_Close(file);
		Reply(c, "ERR %s is not a readable CIA (0x%08lX)", path, (unsigned long)res);
		return;
	}
	const char *why = "";
	// Self-update: install over the running agent without deleting it first;
	// the new version starts the next time the agent is launched.
	const bool self_update = entry.titleID == AGENT_TITLE_ID;
	TitleState state = self_update ? TITLE_NOT_INSTALLED : CheckTitle(entry.titleID, &why);
	if (state == TITLE_REFUSED)
	{
		FSFILE_Close(file);
		Reply(c, "ERR refusing %016llX: %s", (unsigned long long)entry.titleID, why);
		return;
	}
	printf("install %s -> %016llX\n", path, (unsigned long long)entry.titleID);
	// n64to3ds titles keep no system save data (saves live under
	// /3ds/DaedalusX64), so replacing the old installation loses nothing.
	if (state == TITLE_OURS_INSTALLED)
	{
		res = AM_DeleteAppTitle(MEDIATYPE_SD, entry.titleID);
		if (R_FAILED(res))
		{
			FSFILE_Close(file);
			Reply(c, "ERR cannot remove the old installation (0x%08lX)", (unsigned long)res);
			return;
		}
	}
	res = InstallFromFile(file, size);
	FSFILE_Close(file);
	if (R_FAILED(res))
		Reply(c, "ERR install failed (0x%08lX)", (unsigned long)res);
	else
		Reply(c, "OK %016llX", (unsigned long long)entry.titleID);
}

static void CmdLaunch(Conn *c, const char *text)
{
	char *end = NULL;
	u64 title_id = strtoull(text, &end, 16);
	const char *why = "title is not installed";
	if (end == text || *end != '\0' || CheckTitle(title_id, &why) != TITLE_OURS_INSTALLED)
	{
		Reply(c, "ERR refusing to launch %s: %s", text, why);
		return;
	}
	Reply(c, "OK");
	sLaunchTitle = title_id;
	sQuit = true;
}

static void Serve(Conn *c)
{
	char line[LINE_MAX_LEN];
	while (!sQuit && ReadLine(c, line, sizeof(line)))
	{
		char cmd[16] = "", arg[PATH_MAX_LEN] = "";
		unsigned long long size = 0;
		int fields = sscanf(line, "%15s %255s %llu", cmd, arg, &size);
		if (fields < 1)
			continue;
		const bool needs_path = strcmp(cmd, "PUT") == 0 || strcmp(cmd, "GET") == 0 || strcmp(cmd, "LS") == 0
			|| strcmp(cmd, "DEL") == 0 || strcmp(cmd, "INSTALL") == 0;
		const bool read_only = strcmp(cmd, "GET") == 0 || strcmp(cmd, "LS") == 0;
		if (needs_path && (fields < 2 || !(read_only ? PathReadable(arg) : PathWritable(arg))))
		{
			Reply(c, "ERR path not allowed: %s", arg);
			if (strcmp(cmd, "PUT") == 0)
				return;	// the payload cannot be skipped safely; drop the connection
			continue;
		}
		printf("> %s\n", line);
		if (strcmp(cmd, "PING") == 0)
			Reply(c, "OK n64to3ds-agent %s %016llX", AGENT_VERSION, (unsigned long long)AGENT_TITLE_ID);
		else if (strcmp(cmd, "PUT") == 0 && fields == 3)
		{
			if (!CmdPut(c, arg, size))
				return;
		}
		else if (strcmp(cmd, "GET") == 0)
			CmdGet(c, arg);
		else if (strcmp(cmd, "LS") == 0)
			CmdList(c, arg);
		else if (strcmp(cmd, "DEL") == 0)
			CmdDelete(c, arg);
		else if (strcmp(cmd, "INSTALL") == 0)
			CmdInstall(c, arg);
		else if (strcmp(cmd, "TITLES") == 0)
			CmdTitles(c);
		else if (strcmp(cmd, "LAUNCH") == 0 && fields >= 2)
			CmdLaunch(c, arg);
		else if (strcmp(cmd, "EXIT") == 0)
		{
			Reply(c, "OK");
			sQuit = true;
		}
		else
			Reply(c, "ERR unknown command: %s", cmd);
	}
}

// ---------------------------------------------------------------------------

static int OpenListener(void)
{
	int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
	if (fd < 0)
		return -1;
	struct sockaddr_in addr;
	memset(&addr, 0, sizeof(addr));
	addr.sin_family = AF_INET;
	addr.sin_port = htons(AGENT_PORT);
	addr.sin_addr.s_addr = INADDR_ANY;
	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0 || listen(fd, 2) != 0)
	{
		close(fd);
		return -1;
	}
	fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
	return fd;
}

int main(void)
{
	gfxInitDefault();
	// Bottom screen: fixed status (address, keys).  Top screen: command log.
	PrintConsole status, log;
	consoleInit(GFX_BOTTOM, &status);
	consoleInit(GFX_TOP, &log);
	aptSetSleepAllowed(false);
	consoleSelect(&status);
	printf("n64to3ds agent v%s\n\n", AGENT_VERSION);

	u32 *soc_buffer = (u32 *)memalign(0x1000, SOC_BUFFER_SIZE);
	bool net = false;
	bool am = R_SUCCEEDED(amInit());
	mkdir("sdmc:/3ds/n64to3ds-agent", 0777);
	mkdir("sdmc:/cias", 0777);

	if (!am)
		printf("amInit failed: install/launch disabled\n");
	printf("START: quit to HOME Menu\n\n");
	consoleSelect(&log);

	// When a game chain-loads the agent, Wi-Fi is often still reconnecting;
	// keep trying (about once a second) instead of giving up at start-up.
	int listener = -1;
	bool reported_wait = false;
	u32 frames_until_retry = 0;
	while (!sQuit && aptMainLoop())
	{
		hidScanInput();
		if (hidKeysDown() & KEY_START)
			break;
		if (listener < 0 && frames_until_retry-- == 0)
		{
			frames_until_retry = 60;
			if (!net && soc_buffer != NULL)
				net = R_SUCCEEDED(socInit(soc_buffer, SOC_BUFFER_SIZE));
			if (net && gethostid() != 0)
				listener = OpenListener();
			consoleSelect(&status);
			if (listener >= 0)
			{
				struct in_addr ip = { .s_addr = (in_addr_t)gethostid() };
				printf("listening on %s:%d\n", inet_ntoa(ip), AGENT_PORT);
			}
			else if (!reported_wait)
			{
				printf("waiting for the network (Wi-Fi)...\n");
				reported_wait = true;
			}
			consoleSelect(&log);
		}
		if (listener >= 0)
		{
			struct sockaddr_in peer;
			socklen_t peer_len = sizeof(peer);
			int fd = accept(listener, (struct sockaddr *)&peer, &peer_len);
			if (fd >= 0)
			{
				fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) & ~O_NONBLOCK);
				printf("client %s\n", inet_ntoa(peer.sin_addr));
				Conn *c = (Conn *)calloc(1, sizeof(Conn));
				if (c != NULL)
				{
					c->fd = fd;
					Serve(c);
					free(c);
				}
				close(fd);
			}
		}
		gspWaitForVBlank();
	}

	if (listener >= 0)
		close(listener);
	if (net)
		socExit();
	if (am)
		amExit();
	if (sLaunchTitle != 0)
	{
		// The jump happens when the agent exits (handled by libctru's aptExit).
		aptSetChainloader(sLaunchTitle, MEDIATYPE_SD);
	}
	gfxExit();
	return 0;
}

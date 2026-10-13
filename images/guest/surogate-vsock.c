// surogate-vsock: the guest's streams to the host over vsock, where its VM gives it Hyper-V's sockets (an
// AF_VSOCK transport) in place of virtio-serial ports. Node has no AF_VSOCK, and the guest no socat. Two forms:
//
//   surogate-vsock <cid>:<port>:<fd>... -- <program> <arg>...
//     connects a stream to each <cid>:<port>, puts it at the descriptor <fd>, and becomes <program>, by its
//     path: a Plan 9 share's mount (guest/root.ts), whose mount(2) takes its connection as a descriptor
//     (trans=fd), which the kernel keeps once mounted.
//   surogate-vsock <cid>:<port>=<path>...
//     connects a stream to each <cid>:<port>, and relays it to the one connection made to a Unix socket at
//     <path>, mode 0600: the agent's ports (vm/init, guest/port.ts). Node opens them by their paths, close
//     on exec, so nothing the agent runs inherits one. Each socket file goes once connected, and takes no
//     second connection. It exits once every stream has ended both ways.
//
// Each refused connect, and each wrong argument, is said on stderr, and nothing runs.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/vm_sockets.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define MAX_STREAMS 8
#define BUFFER (64 * 1024)

static const char USAGE[] = "usage: surogate-vsock <cid>:<port>:<fd>... -- <program> <arg>... | surogate-vsock <cid>:<port>=<path>...\n";

static void usage(void)
{
	fputs(USAGE, stderr);
	exit(2);
}

// A decimal number of at most UINT_MAX, with no sign and no leading zero, ending at *end*: or usage.
static unsigned number(const char *text, const char *end)
{
	if (text == end || end - text > 10 || (*text == '0' && end - text > 1))
		usage();
	unsigned long long value = 0;
	for (const char *at = text; at < end; at++) {
		if (*at < '0' || *at > '9')
			usage();
		value = value * 10 + (unsigned)(*at - '0');
	}
	if (value > UINT_MAX)
		usage();
	return (unsigned)value;
}

// A stream connected to *cid*:*port*, close on exec; or the words, and the exit.
static int connected(unsigned cid, unsigned port)
{
	struct sockaddr_vm address = { .svm_family = AF_VSOCK, .svm_cid = cid, .svm_port = port };
	int fd = socket(AF_VSOCK, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0 || connect(fd, (struct sockaddr *)&address, sizeof address) != 0) {
		fprintf(stderr, "surogate-vsock: cannot connect to %u:%u: %s\n", cid, port, strerror(errno));
		exit(1);
	}
	return fd;
}

struct stream {
	unsigned cid, port;
	int fd;           // the descriptor form's
	const char *path; // the relay's
};

// One direction of a relayed stream: what was read from *from* and is not yet written to *to*.
struct way {
	int from, to;
	char buffer[BUFFER];
	size_t have, sent;
	int ended; // *from* ended, and all of it was written
};

static int nonblocking(int fd)
{
	int flags = fcntl(fd, F_GETFL);
	return flags < 0 ? -1 : fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

// A Unix socket listening at *path*, mode 0600, for one connection; or the words, and the exit. Nothing that
// is there already is taken for it.
static int listening(const char *path)
{
	struct sockaddr_un address = { .sun_family = AF_UNIX };
	if (strlen(path) >= sizeof address.sun_path || path[0] != '/') {
		fprintf(stderr, "surogate-vsock: %s is not a socket's path\n", path);
		exit(2);
	}
	strcpy(address.sun_path, path);
	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	mode_t was = umask(0177);
	int bound = fd < 0 ? -1 : bind(fd, (struct sockaddr *)&address, sizeof address);
	umask(was);
	if (bound != 0 || listen(fd, 1) != 0 || nonblocking(fd) != 0) {
		fprintf(stderr, "surogate-vsock: cannot listen at %s: %s\n", path, strerror(errno));
		exit(1);
	}
	return fd;
}

// Moves what it can of *way*: 0 while it goes on, 1 once it has ended, -1 when the stream failed.
static int move(struct way *way, short readable, short writable)
{
	if (way->have == way->sent && !way->ended && readable) {
		ssize_t got = read(way->from, way->buffer, sizeof way->buffer);
		if (got < 0)
			return errno == EAGAIN || errno == EINTR ? 0 : -1;
		way->have = (size_t)got;
		way->sent = 0;
		if (got == 0) {
			way->ended = 1;
			shutdown(way->to, SHUT_WR);
			return 1;
		}
	}
	if (way->have > way->sent && writable) {
		ssize_t put = write(way->to, way->buffer + way->sent, way->have - way->sent);
		if (put < 0)
			return errno == EAGAIN || errno == EINTR ? 0 : -1;
		way->sent += (size_t)put;
	}
	return way->ended;
}

static void relay(struct stream *streams, int count)
{
	// A peer gone mid-write is a stream that ended: never a signal that ends every stream.
	signal(SIGPIPE, SIG_IGN);
	int host[MAX_STREAMS], listener[MAX_STREAMS], agent[MAX_STREAMS];
	static struct way out[MAX_STREAMS], in[MAX_STREAMS]; // out: to the host; in: from it
	for (int i = 0; i < count; i++) {
		host[i] = connected(streams[i].cid, streams[i].port);
		if (nonblocking(host[i]) != 0)
			exit(1);
	}
	for (int i = 0; i < count; i++) {
		listener[i] = listening(streams[i].path);
		agent[i] = -1;
	}
	for (int open = count; open > 0;) {
		struct pollfd polled[MAX_STREAMS * 2];
		for (int i = 0; i < count; i++) {
			struct pollfd *a = &polled[2 * i], *h = &polled[2 * i + 1];
			*a = (struct pollfd){ .fd = -1 };
			*h = (struct pollfd){ .fd = -1 };
			if (listener[i] >= 0) {
				*a = (struct pollfd){ .fd = listener[i], .events = POLLIN };
			} else if (agent[i] >= 0) {
				*a = (struct pollfd){ .fd = agent[i], .events = (short)((out[i].have == out[i].sent && !out[i].ended ? POLLIN : 0) | (in[i].have > in[i].sent ? POLLOUT : 0)) };
				*h = (struct pollfd){ .fd = host[i], .events = (short)((in[i].have == in[i].sent && !in[i].ended ? POLLIN : 0) | (out[i].have > out[i].sent ? POLLOUT : 0)) };
			}
		}
		if (poll(polled, (nfds_t)(count * 2), -1) < 0) {
			if (errno == EINTR)
				continue;
			exit(1);
		}
		for (int i = 0; i < count; i++) {
			short a = polled[2 * i].revents, h = polled[2 * i + 1].revents;
			if (listener[i] >= 0) {
				if (!(a & POLLIN))
					continue;
				int taken = accept4(listener[i], NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
				if (taken < 0)
					continue;
				close(listener[i]);
				listener[i] = -1;
				unlink(streams[i].path);
				agent[i] = taken;
				out[i] = (struct way){ .from = taken, .to = host[i] };
				in[i] = (struct way){ .from = host[i], .to = taken };
				continue;
			}
			if (agent[i] < 0)
				continue;
			int sent = move(&out[i], (short)(a & (POLLIN | POLLHUP | POLLERR)), (short)(h & (POLLOUT | POLLERR)));
			int came = move(&in[i], (short)(h & (POLLIN | POLLHUP | POLLERR)), (short)(a & (POLLOUT | POLLERR)));
			if (sent < 0 || came < 0 || (sent == 1 && came == 1)) {
				close(agent[i]);
				close(host[i]);
				agent[i] = -1;
				open--;
			}
		}
	}
	exit(0);
}

int main(int argc, char **argv)
{
	struct stream streams[MAX_STREAMS];
	int count = 0, relayed = 0, at = 1;
	for (; at < argc && strcmp(argv[at], "--") != 0; at++) {
		if (count == MAX_STREAMS)
			usage();
		struct stream *stream = &streams[count++];
		const char *text = argv[at], *colon = strchr(text, ':');
		if (!colon)
			usage();
		stream->cid = number(text, colon);
		const char *rest = colon + 1, *end = rest + strcspn(rest, ":=");
		stream->port = number(rest, end);
		int relays = *end == '=';
		if (count > 1 && relays != relayed)
			usage();
		relayed = relays;
		if (relays) {
			stream->path = end + 1;
		} else {
			if (*end != ':')
				usage();
			unsigned fd = number(end + 1, end + 1 + strlen(end + 1));
			if (fd < 3 || fd > 1023)
				usage();
			stream->fd = (int)fd;
			for (int other = 0; other < count - 1; other++)
				if (streams[other].fd == stream->fd)
					usage();
		}
	}
	if (count == 0)
		usage();
	if (relayed) {
		if (at != argc)
			usage();
		relay(streams, count);
	}
	// The descriptor form: a program, by its path, after the "--".
	if (at + 1 >= argc || argv[at + 1][0] != '/')
		usage();
	for (int i = 0; i < count; i++) {
		// The lowest free descriptor: never one a stream before it was put at. dup2 clears close on exec on
		// the copy; one that is at its own descriptor already is cleared alone.
		int fd = connected(streams[i].cid, streams[i].port);
		if (fd == streams[i].fd) {
			if (fcntl(fd, F_SETFD, 0) != 0)
				exit(1);
		} else {
			if (dup2(fd, streams[i].fd) < 0)
				exit(1);
			close(fd);
		}
	}
	execv(argv[at + 1], &argv[at + 1]);
	fprintf(stderr, "surogate-vsock: cannot run %s: %s\n", argv[at + 1], strerror(errno));
	return 127;
}

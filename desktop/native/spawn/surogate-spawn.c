// surogate-spawn: how the app starts a program that is not its own on macOS (src/clean-child.ts).
//
//   surogate-spawn <keep> <program> [argument ...]
//
// It becomes *program*, by its whole path, with the same process id, the arguments after it and
// this process's environment, holding its descriptors 0 to *keep* that are open without
// close-on-exec, and no other. Linux runs a line of perl for this, which reads /proc/self/fd; macOS
// has its own way, in one call: posix_spawn with POSIX_SPAWN_SETEXEC, which makes the call an exec
// of this very process, and POSIX_SPAWN_CLOEXEC_DEFAULT, which closes every descriptor the file
// actions do not name. Apple's posix_spawn(2): "POSIX_SPAWN_SETEXEC ... the function behaves as
// execve(2) does ... the calling process is replaced"; posix_spawnattr_setflags(3):
// "POSIX_SPAWN_CLOEXEC_DEFAULT ... causes all file descriptors to be closed in the child ... that
// are not specifically named by a file action"; posix_spawn_file_actions_addinherit_np(3): the
// descriptor "is inherited by the new process image", the one action that names a descriptor
// without moving it.
//
// A program that cannot be run is said as a shell says it, and ends 127, as the perl line ends; an
// ask it cannot read ends 126. It runs no shell, looks nothing up on the PATH, and reads nothing.

#include <errno.h>
#include <fcntl.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

extern char **environ;

// The most descriptors a program is meant to keep: the three standard ones and a channel or two.
#define MOST_KEPT 64

static int refuse(const char *why) {
  fprintf(stderr, "surogate-spawn: %s\n", why);
  return 126;
}

int main(int argc, char **argv) {
  if (argc < 3) return refuse("usage: surogate-spawn <keep> <program> [argument ...]");
  char *end = NULL;
  errno = 0;
  long keep = strtol(argv[1], &end, 10);
  if (errno != 0 || end == argv[1] || *end != '\0' || keep < 2 || keep > MOST_KEPT) return refuse("<keep> is a number from 2 to 64");
  const char *program = argv[2];
  if (program[0] != '/') return refuse("<program> is named by its whole path");

  posix_spawnattr_t attributes;
  posix_spawn_file_actions_t actions;
  if (posix_spawnattr_init(&attributes) != 0 || posix_spawn_file_actions_init(&actions) != 0) return refuse("cannot start a program");
  // Its signal mask and dispositions stay as they are, as an exec keeps them: no SETSIGMASK, no SETSIGDEF.
  if (posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETEXEC | POSIX_SPAWN_CLOEXEC_DEFAULT) != 0) return refuse("cannot start a program");
  for (int fd = 0; fd <= keep; fd += 1) {
    // What an exec would keep of them: each that is open and not close-on-exec. One that is not open stays so.
    int flags = fcntl(fd, F_GETFD);
    if (flags == -1 || (flags & FD_CLOEXEC) != 0) continue;
    if (posix_spawn_file_actions_addinherit_np(&actions, fd) != 0) return refuse("cannot start a program");
  }
  // argv[2] onwards: the program's own argv, its first the program's path, as the perl line's exec gives it.
  int failed = posix_spawn(NULL, program, &actions, &attributes, &argv[2], environ);
  // Only a failure returns: with POSIX_SPAWN_SETEXEC a success is this process become the program.
  fprintf(stderr, "%s: %s\n", program, strerror(failed));
  return 127;
}

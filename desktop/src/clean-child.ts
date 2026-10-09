// The one way the app starts a program that is not its own. Chromium opens its files and sockets
// without close-on-exec, and closes them itself in the children it starts; Node's spawn closes
// nothing. So a child started with spawn alone holds what the main process had open: the profile's
// files, open for writing, and its sockets, its own ends of its channels with its other processes
// among them. Node cannot close a descriptor at spawn, and the app cannot mark Chromium's.
//
// A child is therefore started through perl, by a fixed line: it closes every descriptor above the
// last one the child is meant to have, and then becomes the program, with the same process id,
// arguments and environment. Perl, and no shell: a shell puts names of its own into the
// environment of what it runs (PWD, SHLVL), and the app's children are given theirs to the name.
// perl-base is part of every Ubuntu. The environment is always the caller's own naming, never the
// app's: perl reads PERL5OPT and PERL5LIB.

import { type ChildProcess, spawn, type SpawnOptions } from "node:child_process";

const PERL = "/usr/bin/perl";
// Its first argument: the last descriptor the program keeps. What follows: the program, by its whole
// path, and its arguments. A program that cannot be run is said as a shell says it, and ends 127.
const LINE = 'use POSIX (); my $keep = shift; opendir(my $dir, "/proc/self/fd") or exit 126; my @open = grep { /^\\d+$/ && $_ > $keep } readdir($dir); '
  + 'closedir($dir); POSIX::close($_) for @open; exec { $ARGV[0] } @ARGV; print STDERR "$ARGV[0]: $!\\n"; exit 127;';

/** What to run in place of *program* with *args*, so that it holds its first *keep* + 1 descriptors and no other. */
export function cleanly(program: string, args: string[], keep = 2): [file: string, args: string[]] {
  if (!program.startsWith("/")) throw new Error(`${program} is not named by its whole path`);
  return [PERL, ["-e", LINE, "--", String(keep), program, ...args]];
}

/**
 * *program*, by its whole path, started as spawn starts one, holding the descriptors its stdio
 * names (the three standard ones unless *options* names more) and nothing else of this process's.
 */
export function spawnClean(program: string, args: string[], options: SpawnOptions & { env: NodeJS.ProcessEnv }): ChildProcess {
  const [file, argv] = cleanly(program, args, Array.isArray(options.stdio) ? Math.max(options.stdio.length - 1, 2) : 2);
  return spawn(file, argv, options);
}

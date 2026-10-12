// How a folder is looked at in a child process of its own on macOS (folders-held.ts, lookApart).

// BSD's test(1), by its whole path: "-e file True if file exists (regardless of type)", a link looked at through to
// what it leads to, as test(1) looks with stat(2) at all but -h and -L. Not BSD's stat(1): its -L, "If the link is
// broken or the target does not exist, fall back on lstat(2)", answers for a link that leads nowhere (seen on macOS 15).
const TEST = "/bin/test";

/** The program and arguments that look at *folder*, following links as the checks do, and print nothing. */
export const lookCommand = (folder: string): [string, string[]] => [TEST, ["-e", folder]];

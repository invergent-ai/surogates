// How a folder is looked at in a child process of its own on macOS (folders-held.ts, lookApart).

// BSD's stat(1), by its whole path.
const STAT = "/usr/bin/stat";

/** The program and arguments that look at *folder*, following links as the checks do, and print nothing but a line's end. */
export const lookCommand = (folder: string): [string, string[]] => [STAT, ["-L", "-f", "", "--", folder]];

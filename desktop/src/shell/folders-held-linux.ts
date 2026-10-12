// How a folder is looked at in a child process of its own on Linux (folders-held.ts, lookApart).

// coreutils' stat, by its whole path.
const STAT = "/usr/bin/stat";

/** The program and arguments that look at *folder*, following links as the checks do, and print nothing. */
export const lookCommand = (folder: string): [string, string[]] => [STAT, ["-L", "--printf=", "--", folder]];

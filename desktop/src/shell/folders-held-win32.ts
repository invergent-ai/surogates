// How a folder is looked at in a child process of its own on Windows (folders-held.ts, lookApart): surogate-helper.exe's
// look, which opens it through its links and prints nothing (native/windows/helper, look.rs). A folder on a network
// share that has stopped answering holds its open for as long as the share's timeout, in that child alone. And the
// app's PATH as Windows writes one (Microsoft, "Environment Variables", PATH): its entries apart by ";", each a whole
// path on a drive; any other is not looked at.

import { HELPER } from "../native-helper.js";

/** The program and arguments that look at *folder*, following links as the checks do, and print nothing. */
export const lookCommand = (folder: string): [string, string[]] => [HELPER, ["look", folder]];

export const PATHS = { delimiter: ";", whole: (entry: string): boolean => /^[A-Za-z]:[\\/]/.test(entry) };

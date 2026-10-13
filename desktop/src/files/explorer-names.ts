// The names Windows's Explorer acts on when it shows a folder. A file left in a folder can make Explorer
// fetch an icon or a library from a path the file names, and a UNC path sends the user's NTLM hash to that
// server (Microsoft's CVE-2025-24054, through .library-ms). On Windows both layers refuse them: the guest's
// rule a session's writes, where the kernel's line names surogate.names=windows (vm/rule-match.h,
// sg_explorer), and the file tools theirs. Each compared as folded (protect.ts), as a volume that compares
// names without case may compare it.

import { folded } from "./protect.js";

export const EXPLORER_NAMES: readonly string[] = ["desktop.ini", "autorun.inf"];
export const EXPLORER_EXTENSIONS: readonly string[] = [".lnk", ".url", ".scf", ".library-ms", ".searchconnector-ms"];

/** Whether *path*'s last component, folded, is one of EXPLORER_NAMES, or ends with one of EXPLORER_EXTENSIONS. */
export function explorerActsOn(path: string): boolean {
  const leaf = folded(path.split("/").at(-1) ?? "");
  return EXPLORER_NAMES.includes(leaf) || EXPLORER_EXTENSIONS.some((extension) => leaf.endsWith(extension));
}

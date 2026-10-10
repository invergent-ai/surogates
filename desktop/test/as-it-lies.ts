// A folder as it lies on this computer, to compare before and after what a thread does: each name under it with
// its mode, size, times of change and bytes, and where each link leads, no link followed. Its last reads are not
// in it: a thread's copy is made by reading the folder.

import { createHash } from "node:crypto";
import { lstatSync, readdirSync, readFileSync, readlinkSync } from "node:fs";
import { join } from "node:path";

export function asItLies(path: string): Record<string, unknown> {
  const found: Record<string, unknown> = {};
  const walk = (at: string, name: string) => {
    const stat = lstatSync(at);
    found[name] = {
      mode: stat.mode, uid: stat.uid, size: stat.size, mtime: stat.mtimeMs, ctime: stat.ctimeMs,
      ...(stat.isFile() ? { bytes: createHash("sha256").update(readFileSync(at)).digest("hex") } : {}),
      ...(stat.isSymbolicLink() ? { link: readlinkSync(at) } : {}),
    };
    if (stat.isDirectory()) for (const entry of readdirSync(at).sort()) walk(join(at, entry), join(name, entry));
  };
  walk(path, ".");
  return found;
}

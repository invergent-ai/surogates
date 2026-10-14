// Whether a file of the install is an administrator's word on Windows, as the install record is: the MSI writes it in
// %ProgramData%\Surogate (desktop/windows/msi/Surogate.wxs), and it says where every user's app downloads from.
// Windows has no root and no mode bits: what Linux's rootsOwn (vm/image.ts) asks of root's uid and of the mode, this asks
// of the owner and of the access list, through ntat, of the file and of the folder that holds it, each opened never
// through a reparse point. A folder only administrators may change is one no other user may put another file in, or a
// link in place of this one, between this check and its read.

import { closeSync } from "node:fs";
import { win32 } from "node:path";

import { ntat, type NtatError, type NtatOwners } from "./ntat.js";

// Who an administrator's word may be owned by: SYSTEM, which the MSI's server runs as, and the Administrators group.
const OWNERS: ReadonlySet<string> = new Set(["S-1-5-18", "S-1-5-32-544"]);
// Who may change it: those two; TrustedInstaller, Windows's own servicing; and OWNER RIGHTS, an entry for whoever
// owns it, which the owner check makes one of the two.
const WRITERS: ReadonlySet<string> = new Set([
  ...OWNERS, "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464", "S-1-3-4",
]);

function judged(said: NtatOwners, path: string): void {
  if (!OWNERS.has(said.owner)) throw new Error(`${path} is not the installer's: only an administrator may own it`);
  if (said.writers.some((writer) => !WRITERS.has(writer))) throw new Error(`${path} is not the installer's: only administrators may change it`);
}

/**
 * Throws unless *path*, a file by its whole path on a drive (C:\...), and the folder that holds it are an
 * administrator's word: owned by SYSTEM or the Administrators, with no one else let change either, and a file and no
 * reparse point, nor a folder that is one. What Windows cannot open, as a file not there, is thrown as Node's error,
 * its code the errno's name.
 */
export function adminsOwn(path: string): void {
  if (!/^[A-Za-z]:\\/.test(path) || win32.normalize(path) !== path) throw new Error(`${path} is not a whole path on a drive`);
  const k = ntat().constants;
  const share = k.FILE_SHARE_READ | k.FILE_SHARE_WRITE | k.FILE_SHARE_DELETE;
  const opened: number[] = [];
  try {
    const dir = ntat().openRoot(`\\\\?\\${win32.dirname(path)}`);
    opened.push(dir);
    const folder = ntat().reopen(dir, k.READ_CONTROL | k.SYNCHRONIZE, share);
    opened.push(folder);
    judged(ntat().owners(folder), win32.dirname(path));
    let file: number;
    try {
      file = ntat().openAt(dir, win32.basename(path), k.READ_CONTROL | k.FILE_READ_ATTRIBUTES | k.SYNCHRONIZE, k.FILE_OPEN,
        k.FILE_NON_DIRECTORY_FILE, share, 0, false);
    } catch (error) {
      if ((error as NtatError).refused === "link") throw new Error(`${path} is not the installer's: it is a link`);
      throw error;
    }
    opened.push(file);
    judged(ntat().owners(file), path);
  } finally {
    for (const fd of opened.reverse()) closeSync(fd);
  }
}

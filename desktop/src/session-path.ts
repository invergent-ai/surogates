// A session's paths (spec, Section 11, "Sessions in the guest"): the server, the model, the guest and the file host's
// checks name each folder by its path in the guest, POSIX on every OS. Only where a folder is opened on this computer
// is it named by this computer's own path (host-path.ts). Electron-free. files/ is on the agent disk, which holds no
// module of the app's but its own: its modules take node:path's posix by name.

import { posix } from "node:path";

import { forThisOs, notBuilt } from "./platform.js";

export const { basename, dirname, isAbsolute, join, normalize, relative, resolve, sep } = posix;

const same = (path: string): string => path;
const unbuilt = (): never => notBuilt("A folder's path in the guest");

// Windows's: C:\Users\Ana\Documents\shop is /mnt/c/Users/Ana/Documents/shop, the form WSL gives the same folder,
// its drive's letter in small and its names as they are. A path on no drive letter (a network share, \\?\ or \\.\
// namespaces, a path relative to a drive or to the current one) has none: a drive letter is the one name the guest
// and every user of the computer give a folder alike. Nor has a name Windows would read as another folder's (Microsoft,
// "Naming Files, Paths, and Namespaces": "Do not end a file or directory name with a space or a period", and the
// reserved characters < > : " / \ | ? * and 1 through 31), or . and .., so that each way gives the other back.
const DRIVE = /^([A-Za-z]):[\\/]/;
const GUEST_DRIVE = /^\/mnt\/([a-z])(?:\/(.*))?$/;
const READ_AS_ANOTHER = (name: string): boolean => name === "." || name === ".." || /[<>:"\\|?*\0-\x1f]/.test(name) || /[. ]$/.test(name);

function windowsGuestPath(hostPath: string): string {
  const drive = DRIVE.exec(hostPath)?.[1];
  if (drive === undefined) throw new Error(`${hostPath} is not a folder on a drive of this computer`);
  const names = hostPath.slice(3).split(/[\\/]/).filter((name) => name !== "");
  if (names.some(READ_AS_ANOTHER)) throw new Error(`${hostPath} cannot be named in the guest`);
  return ["/mnt", drive.toLowerCase(), ...names].join("/");
}

function windowsHostPath(guestPath: string): string {
  const [, drive, rest = ""] = GUEST_DRIVE.exec(guestPath) ?? [];
  const names = rest.split("/").filter((name) => name !== "");
  if (drive === undefined || names.some(READ_AS_ANOTHER)) throw new Error(`${guestPath} names no path on this computer`);
  return `${drive.toUpperCase()}:\\${names.join("\\")}`;
}

/** A folder's path as the server, the model and the guest name it: the folder's own path on Linux and macOS; /mnt/<drive>/… on Windows. */
export const guestPathOf: (hostPath: string) => string = forThisOs({ linux: same, darwin: same, win32: windowsGuestPath }) ?? unbuilt;

/** The path on this computer of a bound folder's guest path, guestPathOf's inverse: what the folder is opened and shared by. */
export const hostPathOf: (guestPath: string) => string = forThisOs({ linux: same, darwin: same, win32: windowsHostPath }) ?? unbuilt;

// A session's paths (spec, Section 11, "Sessions in the guest"): the server, the model, the guest and the file host's
// checks name each folder by its path in the guest, POSIX on every OS. Only where a folder is opened on this computer
// is it named by this computer's own path (host-path.ts). Electron-free. files/ is on the agent disk, which holds no
// module of the app's but its own: its modules take node:path's posix by name.

import { posix } from "node:path";

import { forThisOs, notBuilt } from "./platform.js";

export const { basename, dirname, isAbsolute, join, normalize, relative, resolve, sep } = posix;

const same = (path: string): string => path;
const unbuilt = (): never => notBuilt("A folder's path in the guest");

/** A folder's path as the server, the model and the guest name it: the folder's own path on Linux and macOS. */
export const guestPathOf: (hostPath: string) => string = forThisOs({ linux: same, darwin: same }) ?? unbuilt;

/** The path on this computer of a bound folder's guest path, guestPathOf's inverse: what the folder is opened and shared by. */
export const hostPathOf: (guestPath: string) => string = forThisOs({ linux: same, darwin: same }) ?? unbuilt;

// Whom the guest's roots run for: this computer's user, by its OS, as the guest's passwd entry and HOME give it.
// Electron-free.

import { homedir, userInfo } from "node:os";

import type { HostUser } from "./guest/protocol.js";
import { forThisOs, notBuilt } from "./platform.js";
import { guestPathOf } from "./session-path.js";

// The user's own uid and gid, their name, and their home.
const own = (): HostUser => {
  const { uid, gid, username } = userInfo();
  return { uid, gid, name: username, home: homedir() };
};

// Windows's, which has no uid: Linux's first user's uid and gid; the user's name in small letters, each character
// that is not one of the portable characters of a Linux user's name (POSIX, "Portable Filename Character Set") made
// _, a leading - too, and user where none of its own is left; and their profile's path in the guest.
const windows = (): HostUser => {
  const name = userInfo().username.toLowerCase().replace(/[^a-z0-9._-]/g, "_").replace(/^-/, "_");
  return { uid: 1000, gid: 1000, name: /[a-z0-9]/.test(name) ? name : "user", home: guestPathOf(homedir()) };
};

/** This computer's user, as the guest's roots run for them. */
export const hostUser: () => HostUser = forThisOs({ linux: own, darwin: own, win32: windows }) ?? (() => notBuilt("Surogate Desktop's user in the sandbox"));

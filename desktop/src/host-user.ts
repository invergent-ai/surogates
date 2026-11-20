// Whom the guest's roots run for: this computer's user, by its OS, as the guest's passwd entry and HOME give it. The home is
// the root's private home at a fixed path, never the user's profile, so that ~/Desktop is not taken for the real one.
// Electron-free.

import { userInfo } from "node:os";

import type { HostUser } from "./guest/protocol.js";
import { forThisOs, notBuilt } from "./platform.js";

/** The root's private home, the same on every OS: a path of its own, above which the user's profile path is not found. */
export const GUEST_HOME = "/surogate/home";

// The user's own uid and gid, their name, and the root's private home.
const own = (): HostUser => {
  const { uid, gid, username } = userInfo();
  return { uid, gid, name: username, home: GUEST_HOME };
};

// Windows's, which has no uid: Linux's first user's uid and gid; the user's name in small letters, each character
// that is not one of the portable characters of a Linux user's name (POSIX, "Portable Filename Character Set") made
// _, a leading - too, and user where none of its own is left; and the root's private home.
const windows = (): HostUser => {
  const name = userInfo().username.toLowerCase().replace(/[^a-z0-9._-]/g, "_").replace(/^-/, "_");
  return { uid: 1000, gid: 1000, name: /[a-z0-9]/.test(name) ? name : "user", home: GUEST_HOME };
};

/** This computer's user, as the guest's roots run for them. */
export const hostUser: () => HostUser = forThisOs({ linux: own, darwin: own, win32: windows }) ?? (() => notBuilt("Surogate Desktop's user in the sandbox"));

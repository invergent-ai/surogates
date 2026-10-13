// Whom the guest's roots run for: this computer's user, by its OS, as the guest's passwd entry and HOME give it.
// Electron-free.

import { homedir, userInfo } from "node:os";

import type { HostUser } from "./guest/protocol.js";
import { forThisOs, notBuilt } from "./platform.js";

// The user's own uid and gid, their name, and their home.
const own = (): HostUser => {
  const { uid, gid, username } = userInfo();
  return { uid, gid, name: username, home: homedir() };
};

/** This computer's user, as the guest's roots run for them. */
export const hostUser: () => HostUser = forThisOs({ linux: own, darwin: own }) ?? (() => notBuilt("Surogate Desktop's user in the sandbox"));

// Whom the guest's roots run for, this computer's user, by its OS (host-user.ts): on Linux and macOS, the user's own
// uid, gid, name and home, as the app has always given them.

import { homedir, userInfo } from "node:os";

import { expect, it } from "vitest";

import { importedOn } from "./platforms.js";

it("is this computer's user as the OS names them on Linux and macOS, and is not built for Windows yet", async () => {
  const { uid, gid, username } = userInfo();
  for (const os of ["linux", "darwin"] as const) {
    const { hostUser } = await importedOn(os, () => import("../src/host-user.js"));
    expect(hostUser(), os).toEqual({ uid, gid, name: username, home: homedir() });
  }
  const { hostUser } = await importedOn("win32", () => import("../src/host-user.js"));
  expect(() => hostUser()).toThrow(new Error("Surogate Desktop's user in the sandbox is not built for Windows yet"));
});

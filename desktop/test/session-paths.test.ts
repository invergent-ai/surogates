// A session's paths are POSIX on every OS (session-path.ts): the server, the model, the guest and the file host's checks
// name each folder by its path in the guest. This computer's own paths (its data folder, its runtime folder, a program)
// are its OS's (host-path.ts).

import { expect, it } from "vitest";

import { importedOn } from "./platforms.js";

it("gives a folder's path in the session as the folder's own on Linux and macOS, either way (Windows's: session-path-win32.test.ts)", async () => {
  for (const os of ["linux", "darwin"] as const) {
    const there = await importedOn(os, () => import("../src/session-path.js"));
    expect(there.guestPathOf("/home/ana/Documents/shop"), os).toBe("/home/ana/Documents/shop");
    expect(there.hostPathOf("/Users/ana/My folder's"), os).toBe("/Users/ana/My folder's");
  }
});

// What the system's backups leave out of the app's data, by this OS (backups.ts): Linux and Windows leave it as it is,
// as neither's own backup takes it (on Windows, File History and OneDrive's folder backup take neither the user's
// local application data); macOS's Time Machine is told to leave out the VM's disks (backups-darwin.test.ts). Another
// OS refuses, in its words, before it keeps the disks in a backup it knows nothing of.

import { existsSync } from "node:fs";

import { expect, it } from "vitest";

import { importedOn } from "./platforms.js";

it("leaves the app's data as it is on Linux and Windows, and makes no folder", async () => {
  for (const os of ["linux", "win32"] as const) {
    const { keepOutOfBackups } = await importedOn(os, () => import("../src/shell/backups.js"));
    await expect(keepOutOfBackups("/nowhere/surogate/vm"), os).resolves.toBeUndefined();
    expect(existsSync("/nowhere"), os).toBe(false);
  }
});

it("refuses on an OS whose backups the app knows nothing of, as a refusal the caller is told", async () => {
  const { keepOutOfBackups } = await importedOn("freebsd", () => import("../src/shell/backups.js"));
  await expect(keepOutOfBackups("/nowhere/surogate/vm")).rejects.toThrow(new Error("Keeping the VM's disks out of this computer's backups is not built for freebsd yet"));
});

// How an update the app checked is put in place on macOS: from the record an administrator placed, by the app's own
// helper.

import { describe, expect, it } from "vitest";

import { importedOn } from "./platforms.js";

describe("an installed app's install on macOS", () => {
  it("is the record an administrator placed, the app's own updates, and its removal from the menu under the user's name", async () => {
    const { installer, darwin } = await importedOn("darwin", async () => ({
      installer: (await import("../src/shell/installer.js")).INSTALLER, darwin: await import("../src/shell/installer-darwin.js"),
    }));
    expect(installer.record()).toBe("/Library/Application Support/Surogate/install.json");
    expect(installer.updates).toBe(darwin.installedUpdates);
    expect(installer.uninstall?.item).toBe("Remove Surogate from This Mac…");
    expect(installer.uninstall?.forget({ root: "/Users/ana/Library/Application Support/Surogate", cache: "/Users/ana/Library/Caches/Surogate", autostartFolder: null, launcher: null }))
      .toEqual(["/Users/ana/Library/Application Support/Surogate", "/Users/ana/Library/Caches/Surogate"]);
  });
});

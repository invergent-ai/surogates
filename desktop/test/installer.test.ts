// How an update the app checked is put in place on Linux: by the install script's root helper, as
// it always has been, from the record the install script left. On Windows, the record the MSI left; on macOS, the
// record an administrator placed, and the app's own helper.

import { describe, expect, it } from "vitest";

import { INSTALLER } from "../src/shell/installer.js";
import { helperRun, installedUpdates } from "../src/shell/installer-linux.js";
import { machineRecord } from "../src/shell/installer-win32.js";
import { importedOn } from "./platforms.js";

describe.runIf(process.platform === "linux")("an update's install on Linux", () => {
  it("is the install script's: its record, an installed app's updates through pkexec, and a test's own helper run as it is", () => {
    expect(INSTALLER.record()).toBe("/etc/surogate/install.json");
    expect(INSTALLER.updates).toBe(installedUpdates);
    expect(INSTALLER.helper).toBe(helperRun);
    // The install script removes the app, never the app itself.
    expect(INSTALLER.uninstall).toBeNull();
  });
});

describe.runIf(process.platform === "win32")("an installed app's record on Windows", () => {
  it("is the MSI's, in %ProgramData%'s Surogate folder, read without a throw as the app starts", () => {
    expect(INSTALLER.record()).toBe(machineRecord());
    expect(INSTALLER.record()).toMatch(/^[A-Za-z]:\\.*\\Surogate\\install\.json$/);
    expect(INSTALLER.uninstall?.item).toBe("Remove Surogate from This Computer…");
    expect(typeof INSTALLER.uninstall?.run).toBe("function");
  });
});

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

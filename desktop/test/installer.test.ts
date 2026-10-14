// How an update the app checked is put in place on Linux: by the install script's root helper, as
// it always has been, from the record the install script left. On Windows, the record the MSI left.

import { describe, expect, it } from "vitest";

import { INSTALLER } from "../src/shell/installer.js";
import { helperRun, installedUpdates } from "../src/shell/installer-linux.js";
import { machineRecord } from "../src/shell/installer-win32.js";

describe.runIf(process.platform === "linux")("an update's install on Linux", () => {
  it("is the install script's: its record, an installed app's updates through pkexec, and a test's own helper run as it is", () => {
    expect(INSTALLER.record()).toBe("/etc/surogate/install.json");
    expect(INSTALLER.updates).toBe(installedUpdates);
    expect(INSTALLER.helper).toBe(helperRun);
  });
});

describe.runIf(process.platform === "win32")("an installed app's record on Windows", () => {
  it("is the MSI's, in %ProgramData%'s Surogate folder, read without a throw as the app starts", () => {
    expect(INSTALLER.record()).toBe(machineRecord());
    expect(INSTALLER.record()).toMatch(/^[A-Za-z]:\\.*\\Surogate\\install\.json$/);
  });
});

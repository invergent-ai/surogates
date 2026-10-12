// How an update the app checked is put in place on Linux: by the install script's root helper, as
// it always has been, from the record the install script left.

import { describe, expect, it } from "vitest";

import { INSTALLER } from "../src/shell/installer.js";
import { helperRun, installedUpdates } from "../src/shell/installer-linux.js";

describe.runIf(process.platform === "linux")("an update's install on Linux", () => {
  it("is the install script's: its record, an installed app's updates through pkexec, and a test's own helper run as it is", () => {
    expect(INSTALLER.record()).toBe("/etc/surogate/install.json");
    expect(INSTALLER.updates).toBe(installedUpdates);
    expect(INSTALLER.helper).toBe(helperRun);
  });
});

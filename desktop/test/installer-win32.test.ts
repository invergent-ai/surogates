// Where an installed app on Windows finds its install record (src/shell/installer-win32.ts): in the folder the MSI makes
// in %ProgramData%, as Windows names it to the app.

import { describe, expect, it } from "vitest";

import { machineRecord } from "../src/shell/installer-win32.js";

describe("the install record on Windows", () => {
  it("is install.json in %ProgramData%'s Surogate folder", () => {
    expect(machineRecord({ ProgramData: "C:\\ProgramData" })).toBe("C:\\ProgramData\\Surogate\\install.json");
    expect(machineRecord({ ProgramData: "D:\\Data\\" })).toBe("D:\\Data\\Surogate\\install.json");
  });

  it("is refused, in words, where Windows names no ProgramData folder on a drive", () => {
    for (const ProgramData of [undefined, "", "ProgramData", "\\\\server\\share", "/var/lib"]) {
      expect(() => machineRecord({ ProgramData })).toThrow("Windows names no ProgramData folder for this app, so it cannot find its install record");
    }
  });
});

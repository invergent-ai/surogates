// What an installed app on Windows reads of its install, and how it runs an update (src/shell/installer-win32.ts): its
// record, in the folder the MSI makes in %ProgramData%, as Windows names it to the app; the release keys and the helper
// beside its own program; and surogate-helper.exe's update, here a stand-in that says what the helper says.

import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { installedUpdates, machineRecord, removeRun, updateRun } from "../src/shell/installer-win32.js";

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

describe("an installed app's updates on Windows", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("read the MSI's record, which marks the version installed too, and the release keys and the helper of the install", () => {
    vi.stubEnv("ProgramData", "C:\\ProgramData");
    const options = installedUpdates("1.2.3", "C:\\Users\\ana\\AppData\\Local\\Surogate\\updates", fetch, new AbortController().signal, "C:\\Program Files\\Surogate");
    expect(options).toMatchObject({
      version: "1.2.3", record: "C:\\ProgramData\\Surogate\\install.json", rootOwned: true, installed: "C:\\ProgramData\\Surogate\\install.json",
      helper: "C:\\Program Files\\Surogate\\resources\\release-keys", cache: "C:\\Users\\ana\\AppData\\Local\\Surogate\\updates",
    });
  });
});

// A stand-in for surogate-helper.exe, a script: it writes its arguments down, then says *out* on stdout and *err* on
// stderr, and ends *code*, or sleeps a while where *code* is null, as the helper waits for the apply.
describe.runIf(process.platform !== "win32")("running an update with the helper", () => {
  let dir: string;
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  function helper(out: string, err: string, code: number | null): string {
    dir = mkdtempSync(join(tmpdir(), "update-run-"));
    const script = join(dir, "helper");
    writeFileSync(script, `#!/bin/sh\nprintf '%s\\n' "$@" > "${dir}/args"\nprintf '${out}'\nprintf '${err}' >&2\n${code === null ? "sleep 5" : `exit ${code}`}\n`);
    chmodSync(script, 0o755);
    return script;
  }

  const files = { manifest: "/u/updates/1.4.0/manifest.json", signature: "/u/updates/1.4.0/manifest.json.sig", tarball: "/u/updates/1.4.0/release.msi" };

  it("hands the helper this app's process, the release's folder and its version, and answers once it says an administrator approved", async () => {
    const applied = await updateRun(helper("approved\\r\\n", "", null), 4242)(files);
    expect(applied).toEqual({ code: 0, said: "", restarts: true });
    expect(readFileSync(join(dir, "args"), "utf8").split("\n").slice(0, 7)).toEqual(["update", "--app-pid", "4242", "--staged", "/u/updates/1.4.0", "--version", "1.4.0"]);
  });

  it("hands a removal this app's process and the folders to forget after it, and answers once an administrator approved", async () => {
    const applied = await removeRun(helper("approved\\n", "", null), 77)(["C:\\Users\\ana\\AppData\\Local\\Surogate"]);
    expect(applied).toEqual({ code: 0, said: "", restarts: true });
    expect(readFileSync(join(dir, "args"), "utf8").split("\n").slice(0, 5)).toEqual(["remove", "--app-pid", "77", "--forget", "C:\\Users\\ana\\AppData\\Local\\Surogate"]);
    expect(await removeRun(helper("", "No administrator approved it", 3), 77)([])).toEqual({ code: 3, said: "No administrator approved it" });
    expect(readFileSync(join(dir, "args"), "utf8").split("\n").slice(0, 3)).toEqual(["remove", "--app-pid", "77"]);
  });

  it("answers how the helper ended, with what it said, where no administrator approved or it could not ask", async () => {
    expect(await updateRun(helper("", "No administrator approved the update", 3))(files)).toEqual({ code: 3, said: "No administrator approved the update" });
    expect(await updateRun("/no/such/helper")(files)).toMatchObject({ code: null });
  });
});

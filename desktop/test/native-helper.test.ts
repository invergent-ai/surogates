// The app's own native helper, per OS (native-helper.ts): where Windows's surogate-helper.exe is, as the installed
// app lays it out and as a development build makes it, and how it is started there. macOS's is pinned by
// mac-helper.test.ts.

import { describe, expect, it, vi } from "vitest";

import { importedOn } from "./platforms.js";

it("is in the installed app's bin folder on Windows, and its build's own in a development build", async () => {
  const { helperAt } = await importedOn("win32", () => import("../src/native-helper.js"));
  expect(helperAt("C:\\Program Files\\Surogate\\resources\\app\\dist")).toBe("C:\\Program Files\\Surogate\\bin\\surogate-helper.exe");
  expect(helperAt("D:\\Apps\\Surogate\\resources\\app\\dist")).toBe("D:\\Apps\\Surogate\\bin\\surogate-helper.exe");
  expect(helperAt("C:\\Users\\ana\\surogates\\desktop\\dist")).toBe("C:\\Users\\ana\\surogates\\desktop\\native\\windows\\target\\release\\surogate-helper.exe");
  // Only the app's own code in an install: no other folder of one.
  expect(helperAt("C:\\Program Files\\Surogate\\resources\\other\\dist")).toBe("C:\\Program Files\\Surogate\\resources\\other\\native\\windows\\target\\release\\surogate-helper.exe");
});

describe("a run of surogate-helper.exe on Windows", () => {
  // What the app hands child_process to start the helper with, as it is asked.
  async function windowsStart(run: (helper: typeof import("../src/native-helper.js")) => unknown) {
    const asked: Array<{ file: string; args: string[]; env: unknown; windowsHide: unknown }> = [];
    vi.doMock("node:child_process", async (actual) => ({
      ...(await actual<typeof import("node:child_process")>()),
      spawnSync: (file: string, args: string[], options: { env: unknown; windowsHide?: unknown }) => {
        asked.push({ file, args, env: options.env, windowsHide: options.windowsHide });
        return { status: 0, stdout: "answered", stderr: "" };
      },
      execFile: (file: string, args: string[], options: { env: unknown; windowsHide?: unknown }, done: (error: null, stdout: string, stderr: string) => void) => {
        asked.push({ file, args, env: options.env, windowsHide: options.windowsHide });
        done(null, "answered", "");
      },
    }));
    const kept = process.env.SystemRoot;
    process.env.SystemRoot = "C:\\Windows";
    try {
      const helper = await importedOn("win32", () => import("../src/native-helper.js"));
      return { said: run(helper), asked };
    } finally {
      if (kept === undefined) delete process.env.SystemRoot;
      else process.env.SystemRoot = kept;
      vi.doUnmock("node:child_process");
    }
  }

  it("starts the helper itself, the one program through which every other is started there, with no window and no environment of the app's but the Windows folder", async () => {
    const program = "C:\\Program Files\\Surogate\\bin\\surogate-helper.exe";
    const chrome = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
    const { said, asked } = await windowsStart(({ helperSync }) => helperSync(["verify", chrome], program));
    expect(said).toBe("answered");
    const { said: later, asked: askedLater } = await windowsStart(({ helper }) => helper(["version", chrome], program));
    expect(await later).toBe("answered");
    // A run waited for and one that is not: each with no console window, which an app started from the Start menu
    // would otherwise give it.
    for (const run of [...asked, ...askedLater]) expect(run).toEqual({ file: program, args: run.args, env: { SystemRoot: "C:\\Windows" }, windowsHide: true });
    expect([...asked, ...askedLater].map(({ args }) => args)).toEqual([["verify", chrome], ["version", chrome]]);
  });

  it("starts none named by a path that is not whole", async () => {
    const { said, asked } = await windowsStart(({ helperSync }) => {
      try {
        return helperSync(["trust"], "bin\\surogate-helper.exe");
      } catch (error) {
        return (error as Error).message;
      }
    });
    expect(said).toBe("bin\\surogate-helper.exe is not named by its whole path");
    expect(asked).toEqual([]);
  });
});

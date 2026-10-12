// How the browser host looks at a Mac's processes and temporary folder: the processes on a profile by the
// arguments surogate-helper lists, as Linux's are by /proc's command lines; and this process's own
// temporary folder for the browser.

import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { browserTemp, holding, listedIn } from "../src/browser/processes-darwin.js";
import { spawnsCleanly } from "./clean-spawns.js";
import { importedOn } from "./platforms.js";

const PROFILE = "/Users/ana/Library/Application Support/Surogate/browser-profiles/0123/chrome";
const line = (pid: number, ...args: string[]) => JSON.stringify({ pid, path: args[0] ?? "", args });

describe("the processes on a profile, on a Mac", () => {
  it("are those that name it in their arguments, whole or as a folder above a path, by pid", async () => {
    const listed = [
      line(812, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", `--user-data-dir=${PROFILE}`),
      line(77, "/Applications/Google Chrome.app/Contents/Frameworks/Helper", "--type=renderer", `--user-data-dir=${PROFILE}`),
      line(90, "/bin/cat", `${PROFILE}/Default/Preferences`),
      line(91, "/bin/echo", `${PROFILE} and more`),
      // Another profile whose name begins as this one's does, and a process that names none.
      line(500, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", `--user-data-dir=${PROFILE}-2`),
      line(501, "/usr/sbin/cron"),
    ].join("\n");
    expect(await holding(PROFILE, async () => `${listed}\n`)).toEqual([77, 90, 91, 812]);
  });

  it("are none where the helper gives no answer, as Linux finds none where /proc is not", async () => {
    expect(await holding(PROFILE, () => Promise.reject(new Error("surogate-helper procs failed")))).toEqual([]);
  });

  it("are read from lines of the helper's own shape alone", () => {
    expect(listedIn([line(1, "/sbin/launchd"), "not json", JSON.stringify({ pid: "2", path: "/x", args: [] }), JSON.stringify({ pid: 3, path: "/x", args: [4] }),
      JSON.stringify({ pid: -4, path: "/x", args: [] }), JSON.stringify({ pid: 5, path: "/x" }), ""].join("\n"))).toEqual([{ pid: 1, path: "/sbin/launchd", args: ["/sbin/launchd"] }]);
  });
});

it("gives the browser this process's own temporary folder, whatever the environment names", () => {
  expect([browserTemp({}), browserTemp({ XDG_RUNTIME_DIR: "/run/user/501" })]).toEqual([tmpdir(), tmpdir()]);
});

it("are a Mac's", async () => {
  const mac = await importedOn("darwin", () => import("../src/browser/processes.js"));
  expect(mac.browserTemp({ XDG_RUNTIME_DIR: "/run/user/501" })).toBe(tmpdir());
});

// The helper started as the app starts it: waits for macOS's clean start of a program (clean-child.ts).
describe.runIf(process.platform === "darwin" && spawnsCleanly)("the processes on a profile, read on a Mac", () => {
  it("are found through surogate-helper procs", async () => {
    const profile = mkdtempSync(join(tmpdir(), "a profile "));
    // A program that takes any argument and waits, as BSD's sleep takes none but its time.
    const waiting = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)", `--user-data-dir=${profile}`], { stdio: "ignore" });
    try {
      await new Promise((resolve) => waiting.once("spawn", resolve));
      expect(await holding(profile)).toEqual([waiting.pid]);
    } finally {
      waiting.kill();
      rmSync(profile, { recursive: true, force: true });
    }
  });
});

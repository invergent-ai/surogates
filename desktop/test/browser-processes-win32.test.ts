// How the browser host looks at a Windows PC's processes and temporary folder: the processes on a profile by the
// arguments surogate-helper.exe lists, as Linux's are by /proc's command lines; and this process's own temporary
// folder for the browser.

import { tmpdir } from "node:os";

import { describe, expect, it } from "vitest";

import { browserTemp, holding } from "../src/browser/processes-win32.js";
import { importedOn } from "./platforms.js";

const PROFILE = "C:\\Users\\Ana\\AppData\\Local\\Surogate\\browser-profiles\\0123\\chrome";
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const line = (pid: number, ...args: string[]) => JSON.stringify({ pid, path: args[0] ?? "", args });

describe("the processes on a profile, on Windows", () => {
  it("are those that name it in their arguments, whole or as a folder above a path, by pid", async () => {
    const listed = [
      line(812, CHROME, `--user-data-dir=${PROFILE}`),
      line(77, CHROME, "--type=renderer", `--user-data-dir=${PROFILE}`),
      line(90, "C:\\Windows\\System32\\notepad.exe", `${PROFILE}\\Default\\Preferences`),
      line(91, "C:\\Windows\\System32\\cmd.exe", `${PROFILE} and more`),
      line(500, CHROME, `--user-data-dir=${PROFILE}-2`),
      line(501, "C:\\Windows\\explorer.exe"),
    ].join("\r\n");
    expect(await holding(PROFILE, async () => `${listed}\r\n`)).toEqual([77, 90, 91, 812]);
  });

  it("are none where the helper gives no answer", async () => {
    expect(await holding(PROFILE, () => Promise.reject(new Error("surogate-helper.exe procs failed")))).toEqual([]);
  });

  it("keep the browser's temporary files in this process's own temporary folder", async () => {
    expect(browserTemp({ TMPDIR: "/elsewhere" })).toBe(tmpdir());
    const windows = await importedOn("win32", () => import("../src/browser/processes.js"));
    expect(windows.browserTemp({})).toBe(tmpdir());
  });
});

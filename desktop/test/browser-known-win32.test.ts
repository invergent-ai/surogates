// The browsers the app knows on Windows: each one's program where its installer puts it, for every user of the PC or
// for this one, in Settings' order, then where its App Paths key says it is; refused where it is no program, is on a
// network volume, or its Authenticode signature does not check out or names another than its maker, which is checked
// once, apart, until the program changes. Custom… takes an .exe.

import { mkdtempSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, win32 } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { Disk } from "../src/browser/choose.js";
import {
  CHECKING, fileStamp, NOT_A_PROGRAM, NOT_SIGNED, ON_A_NETWORK, PUBLISHERS, type ProgramTools, UNCHECKED, windowsBrowsers,
} from "../src/browser/known-win32.js";
import { importedOn } from "./platforms.js";

const ENV = { ProgramFiles: "C:\\Program Files", "ProgramFiles(x86)": "C:\\Program Files (x86)", LOCALAPPDATA: "C:\\Users\\Ana\\AppData\\Local" };

// Windows's tools in a test: App Paths as *registered* gives them; each signature as *signed* says, once the test lets it be said.
function tools(registered: Record<string, string[]> = {}, signed: (path: string) => { trusted: boolean; signer: string | null; remote: boolean } = () => ({ trusted: true, signer: "Google LLC", remote: false })) {
  const checks: string[] = [];
  const asked: string[] = [];
  const waiting: Array<() => void> = [];
  const held = { on: false };
  const own: ProgramTools = {
    appPaths: (name) => {
      asked.push(name);
      return registered[name] ?? [];
    },
    verify: async (path) => {
      checks.push(path);
      if (held.on) await new Promise<void>((go) => waiting.push(go));
      return signed(path);
    },
    stamp: (path) => path,
  };
  return { own, checks, asked, held, release: () => waiting.splice(0).forEach((go) => go()) };
}

const disk: Disk = { realpath: (path) => path, executable: () => true, head: () => "" };

describe("where the app looks for each browser on Windows", () => {
  it("is each one's installer's places, for every user and for this one, in Settings' order, then its App Paths key's", () => {
    const { own } = tools({ "chrome.exe": ["D:\\Apps\\Chrome\\chrome.exe"], "msedge.exe": ["C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"] });
    const candidates = windowsBrowsers(own, ENV).candidates("C:\\Users\\Ana");
    expect(candidates.map(({ id, name, verified }) => [id, name, verified])).toEqual([
      ["chrome", "Google Chrome", true], ["edge", "Microsoft Edge", true], ["brave", "Brave", false], ["vivaldi", "Vivaldi", false], ["chromium", "Chromium", false],
    ]);
    expect(Object.fromEntries(candidates.map(({ id, paths }) => [id, paths]))).toEqual({
      chrome: [
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Users\\Ana\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe", "D:\\Apps\\Chrome\\chrome.exe",
      ],
      // Its App Paths key's path is one of its places already: said once.
      edge: ["C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe", "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"],
      brave: [
        "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe", "C:\\Program Files (x86)\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
        "C:\\Users\\Ana\\AppData\\Local\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
      ],
      vivaldi: ["C:\\Users\\Ana\\AppData\\Local\\Vivaldi\\Application\\vivaldi.exe", "C:\\Program Files\\Vivaldi\\Application\\vivaldi.exe"],
      // Chromium's program is named as Chrome's: Chrome's App Paths key is Chrome's.
      chromium: ["C:\\Users\\Ana\\AppData\\Local\\Chromium\\Application\\chrome.exe"],
    });
  });

  it("leaves out a folder the environment names relatively, on no drive, or not at all, and the user's own under their profile stands in for theirs", () => {
    const { own } = tools();
    const candidates = windowsBrowsers(own, { ProgramFiles: "Program Files", "ProgramFiles(x86)": "\\\\server\\apps" }).candidates("C:\\Users\\Ana");
    expect(candidates.find(({ id }) => id === "chrome")?.paths).toEqual(["C:\\Users\\Ana\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe"]);
  });

  it("asks App Paths again only once its answer is half a minute old", () => {
    const { own, asked } = tools();
    const known = windowsBrowsers(own, ENV);
    known.candidates("C:\\Users\\Ana");
    known.candidates("C:\\Users\\Ana");
    expect(asked).toEqual(["chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe"]);
  });
});

describe("a browser the app drives on Windows", () => {
  const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

  it("is a program whose signature checks out and names its maker, said once it is checked, and checked once while it stays the same", async () => {
    const { own, checks } = tools();
    const known = windowsBrowsers(own, ENV);
    expect(known.refused(CHROME, disk)).toBe(CHECKING);
    await known.checked(CHROME);
    expect(known.refused(CHROME, disk)).toBeNull();
    expect(known.refused(CHROME, disk)).toBeNull();
    expect(checks).toEqual([CHROME]);
  });

  it("is checked again once its program changes, as an update replaces it", async () => {
    const { own, checks } = tools();
    let now = "first";
    const known = windowsBrowsers({ ...own, stamp: () => now }, ENV);
    await known.checked(CHROME);
    await known.checked(CHROME);
    now = "updated";
    await known.checked(CHROME);
    expect(checks).toEqual([CHROME, CHROME]);
  });

  it("tells a program changed by its size, its change time and its making", () => {
    const dir = mkdtempSync(join(tmpdir(), "windows-browsers-"));
    try {
      const program = join(dir, "chrome.exe");
      writeFileSync(program, "MZ");
      const before = fileStamp(program);
      utimesSync(program, new Date(0), new Date(1_000));
      expect(fileStamp(program)).not.toBe(before);
      expect(fileStamp(join(dir, "gone.exe"))).toBe("none");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("is not one whose signature does not check out, names another than its maker, could not be checked, or that is on a network volume", async () => {
    const verdicts: Record<string, { trusted: boolean; signer: string | null; remote: boolean } | Error> = {
      "C:\\a\\chrome.exe": { trusted: false, signer: null, remote: false },
      "C:\\b\\chrome.exe": { trusted: true, signer: "Someone Else Ltd", remote: false },
      "C:\\c\\msedge.exe": { trusted: true, signer: "Google LLC", remote: false },
      "Z:\\apps\\chrome.exe": { trusted: true, signer: "Google LLC", remote: true },
      "C:\\d\\chrome.exe": new Error("surogate-helper.exe verify failed: no answer"),
    };
    const own: ProgramTools = {
      appPaths: () => [],
      verify: async (path) => {
        const verdict = verdicts[path]!;
        if (verdict instanceof Error) throw verdict;
        return verdict;
      },
      stamp: (path) => path,
    };
    const known = windowsBrowsers(own, ENV);
    for (const path of Object.keys(verdicts)) await known.checked(path);
    expect(Object.keys(verdicts).map((path) => known.refused(path, disk))).toEqual([NOT_SIGNED, NOT_SIGNED, NOT_SIGNED, ON_A_NETWORK, UNCHECKED]);
  });

  it("is any signed program's in Custom…, as no maker is known for it, but never a file that is not a program or one on a share", async () => {
    const { own } = tools({}, () => ({ trusted: true, signer: "A Browser Maker", remote: false }));
    const known = windowsBrowsers(own, ENV);
    await known.checked("C:\\Tools\\other-browser.exe");
    expect(known.refused("C:\\Tools\\other-browser.exe", disk)).toBeNull();
    expect(known.refused("C:\\Tools\\other-browser.cmd", disk)).toBe(NOT_A_PROGRAM);
    expect(known.refused("\\\\server\\apps\\chrome.exe", disk)).toBe(ON_A_NETWORK);
    expect(known.programOf("C:\\Tools\\other-browser.exe", disk)).toBe("C:\\Tools\\other-browser.exe");
    expect(known.programOf("C:\\gone.exe", { ...disk, realpath: () => { throw new Error("ENOENT"); } })).toBeNull();
  });

  it("names each known browser's maker as its signature does", () => {
    expect(PUBLISHERS).toEqual({ "chrome.exe": "Google LLC", "msedge.exe": "Microsoft Corporation", "brave.exe": "Brave Software, Inc.", "vivaldi.exe": "Vivaldi Technologies AS" });
  });

  it("is picked in Custom… among programs alone", async () => {
    const { pickFilters } = await importedOn("win32", () => import("../src/browser/choose.js"));
    expect(pickFilters()).toEqual([{ name: "Programs", extensions: ["exe"] }]);
    for (const os of ["linux", "darwin"] as const) expect((await importedOn(os, () => import("../src/browser/choose.js"))).pickFilters(), os).toEqual([]);
  });

  it("is a Windows PC's", async () => {
    const { KNOWN_BROWSERS } = await importedOn("win32", () => import("../src/browser/known.js"));
    expect(KNOWN_BROWSERS.refused("C:\\Tools\\notes.txt", disk)).toBe(NOT_A_PROGRAM);
    expect(win32.isAbsolute(KNOWN_BROWSERS.candidates("C:\\Users\\Ana")[0]!.paths[0] ?? "C:\\")).toBe(true);
  });
});

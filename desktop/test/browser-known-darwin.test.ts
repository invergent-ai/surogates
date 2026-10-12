// The browsers the app knows on macOS: each one's app in /Applications or ~/Applications, its program the
// one its Info.plist names; refused on a disk image, in a temporary copy of macOS's, outside an app, or
// where its signature does not check out, which is checked once, apart, until the app changes.

import { execFileSync } from "node:child_process";
import { chmodSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { chosenBrowser, Disk } from "../src/browser/choose.js";
import {
  type AppTools, APPS, appOf, CHECKING, darwinBrowsers, NOT_AN_APP, NOT_SIGNED, ON_A_VOLUME, SYSTEM, TRANSLOCATED,
} from "../src/browser/known-darwin.js";
import { spawnsCleanly } from "./clean-spawns.js";
import { importedOn } from "./platforms.js";

let home: string;

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "mac-browsers-"));
});

afterEach(() => {
  rmSync(home, { recursive: true, force: true });
});

// An app at *app* whose Info.plist names *program*, there to run.
function app(at: string, program: string): string {
  mkdirSync(join(at, "Contents", "MacOS"), { recursive: true });
  writeFileSync(join(at, "Contents", "Info.plist"), `<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>${program}</string>
<key>CFBundleIdentifier</key><string>test.surogate.${program.replaceAll(" ", "-")}</string>
<key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
`);
  writeFileSync(join(at, "Contents", "MacOS", program), "#!/bin/sh\n");
  chmodSync(join(at, "Contents", "MacOS", program), 0o755);
  return join(at, "Contents", "MacOS", program);
}

// The app's tools in a test: Info.plists read as a test writes them, each read counted; each signature
// as *signed* says, once the test lets it be said.
function tools(signed: (app: string) => string | null = () => null) {
  const reads: string[] = [];
  const checks: string[] = [];
  const waiting: Array<() => void> = [];
  const held = { on: false };
  const own: AppTools = {
    executableIn: (plist) => {
      reads.push(plist);
      const named = /<key>CFBundleExecutable<\/key>\s*<string>([^<]*)<\/string>/.exec(readFileSync(plist, "utf8"));
      if (!named) throw new Error(`${plist} names no program`);
      return named[1]!;
    },
    verify: async (at) => {
      checks.push(at);
      if (held.on) await new Promise<void>((go) => waiting.push(go));
      return signed(at);
    },
  };
  const release = () => {
    held.on = false;
    for (const go of waiting.splice(0)) go();
  };
  return { own, reads, checks, hold: () => void (held.on = true), release };
}

// This computer's disk, as it is.
const DISK: Disk = {
  realpath: (path) => path,
  executable: () => true,
  head: () => "",
};

describe("the browsers the app knows on macOS", () => {
  it("are each one's app, in Applications and then in the user's own, each at the program its Info.plist names", () => {
    const own = join(home, "Applications");
    const edge = app(join(own, "Microsoft Edge.app"), "Microsoft Edge");
    const vivaldi = app(join(own, "Vivaldi.app"), "Vivaldi Browser");
    const { own: given } = tools();
    const known = darwinBrowsers(given).candidates(home);
    expect(known.map(({ id, name, verified }) => [id, name, verified])).toEqual(APPS.map(({ id, name, verified }) => [id, name, verified]));
    expect(known.find(({ id }) => id === "edge")?.paths.at(-1)).toBe(edge);
    expect(known.find(({ id }) => id === "vivaldi")?.paths.at(-1)).toBe(vivaldi);
    // Where an app is in neither folder, no path at all.
    expect(known.filter(({ paths }) => paths.some((path) => path.startsWith(own))).map(({ id }) => id)).toEqual(["edge", "vivaldi"]);
  });

  it("read an app's Info.plist once while it stays the same file, and again once it changes", () => {
    const at = join(home, "Applications", "Chromium.app");
    app(at, "Chromium");
    const { own, reads } = tools();
    const known = darwinBrowsers(own);
    known.candidates(home);
    known.candidates(home);
    expect(reads.filter((plist) => plist.startsWith(home))).toHaveLength(1);
    const plist = join(at, "Contents", "Info.plist");
    writeFileSync(plist, readFileSync(plist, "utf8").replace("<string>Chromium</string>", "<string>Chromium2</string>"));
    utimesSync(plist, new Date(), new Date(Date.now() + 5_000));
    expect(known.candidates(home).find(({ id }) => id === "chromium")?.paths.at(-1)).toBe(join(at, "Contents", "MacOS", "Chromium2"));
  });

  it("name no program for an app whose Info.plist names none, or one outside its own folder", () => {
    for (const named of ["", "../../../../bin/sh", ".."]) {
      const at = join(home, "Applications", "Brave Browser.app");
      rmSync(at, { recursive: true, force: true });
      app(at, "Brave Browser");
      writeFileSync(join(at, "Contents", "Info.plist"), `<plist><dict><key>CFBundleExecutable</key><string>${named}</string></dict></plist>`);
      expect(darwinBrowsers(tools().own).candidates(home).find(({ id }) => id === "brave")?.paths.filter((path) => path.startsWith(home))).toEqual([]);
    }
  });

  it("refuse a program outside an app, an app on a disk image or another volume, and one in macOS's temporary copy", () => {
    const known = darwinBrowsers(tools().own);
    expect(known.refused("/usr/local/bin/chromium", DISK)).toBe(NOT_AN_APP);
    expect(known.refused("/Applications/Google Chrome.app/Contents/Frameworks/x", DISK)).toBe(NOT_AN_APP);
    expect(known.refused("/Volumes/Google Chrome/Google Chrome.app/Contents/MacOS/Google Chrome", DISK)).toBe(ON_A_VOLUME);
    expect(known.refused("/private/var/folders/xy/abc/T/AppTranslocation/0A1B/d/Google Chrome.app/Contents/MacOS/Google Chrome", DISK)).toBe(TRANSLOCATED);
    expect(appOf("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")).toBe("/Applications/Google Chrome.app");
    expect(appOf("/Applications/Google Chrome.app/Contents/Frameworks/Helper.app/Contents/MacOS/Helper")).toBe("/Applications/Google Chrome.app/Contents/Frameworks/Helper.app");
  });

  it("are refused while their signature is checked, apart, and then as it was found, once until the app changes", async () => {
    const chrome = app(join(home, "Applications", "Google Chrome.app"), "Google Chrome");
    const brave = app(join(home, "Applications", "Brave Browser.app"), "Brave Browser");
    const { own, checks, hold, release } = tools((at) => (at.includes("Brave") ? NOT_SIGNED : null));
    hold();
    const known = darwinBrowsers(own);
    expect([known.refused(chrome, DISK), known.refused(brave, DISK)]).toEqual([CHECKING, CHECKING]);
    const done = Promise.all([known.checked(chrome), known.checked(brave)]);
    release();
    await done;
    expect([known.refused(chrome, DISK), known.refused(brave, DISK)]).toEqual([null, NOT_SIGNED]);
    expect(checks).toEqual([join(home, "Applications", "Google Chrome.app"), join(home, "Applications", "Brave Browser.app")]);
    // Checked again only once the app's program has changed, as an update changes it.
    known.refused(chrome, DISK);
    expect(checks).toHaveLength(2);
    utimesSync(chrome, new Date(), new Date(Date.now() + 5_000));
    expect(known.refused(chrome, DISK)).toBe(CHECKING);
    await known.checked(chrome);
    expect(checks).toHaveLength(3);
  });

  it("are refused in the words of a check that could not be run", async () => {
    const chrome = app(join(home, "Applications", "Google Chrome.app"), "Google Chrome");
    const known = darwinBrowsers({ ...tools().own, verify: () => Promise.reject(new Error("Starting a program cleanly is not built for macOS yet")) });
    known.refused(chrome, DISK);
    await known.checked(chrome);
    expect(known.refused(chrome, DISK)).toBe("Starting a program cleanly is not built for macOS yet");
  });

  it("take an app picked in Custom… as its program, and a program as it is", () => {
    const own = join(home, "Applications");
    const program = app(join(own, "Chrome Canary.app"), "Google Chrome Canary");
    symlinkSync(join(own, "Chrome Canary.app"), join(home, "Canary.app"));
    const disk: Disk = { ...DISK, realpath: (path) => {
      if (path === join(home, "Canary.app")) return join(own, "Chrome Canary.app");
      if (path.includes("gone")) throw new Error("ENOENT");
      return path;
    } };
    const known = darwinBrowsers(tools().own);
    expect(known.programOf(join(own, "Chrome Canary.app"), disk)).toBe(program);
    expect(known.programOf(join(home, "Canary.app"), disk)).toBe(program);
    expect(known.programOf(program, disk)).toBe(program);
    expect(known.programOf(join(home, "gone.app"), disk)).toBeNull();
    expect(known.programOf(join(home, "Empty.app"), disk)).toBeNull();
  });

  it("are a Mac's, found and chosen as on Linux once their checks are done", async () => {
    const { KNOWN_BROWSERS } = await importedOn("darwin", () => import("../src/browser/known.js"));
    expect(KNOWN_BROWSERS.candidates(home).map(({ id }) => id)).toEqual(["chrome", "edge", "brave", "vivaldi", "chromium"]);
    expect(KNOWN_BROWSERS.refused("/usr/bin/chromium", DISK)).toBe(NOT_AN_APP);
  });
});

it("keep no browser picked in Custom… that a Mac refuses", async () => {
  const choose = await importedOn("darwin", () => import("../src/browser/choose.js"));
  expect((choose.chosenBrowser as typeof chosenBrowser)({ choice: "custom", executable: "/usr/bin/chromium", version: "" }, [], DISK)).toBeNull();
  expect(await choose.unsupportedOnceChecked("/Volumes/Chromium/Chromium.app/Contents/MacOS/Chromium", DISK)).toBe(ON_A_VOLUME);
});

// codesign and plutil started as the app starts them: wait for macOS's clean start of a program (clean-child.ts).
describe.runIf(process.platform === "darwin" && spawnsCleanly)("the browsers the app knows on a Mac", () => {
  it("find this Mac's Chrome in /Applications, its signature checking out", async () => {
    const known = darwinBrowsers(SYSTEM);
    const chrome = known.candidates(home).find(({ id }) => id === "chrome")!;
    expect(chrome.paths[0]).toBe("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome");
    known.refused(chrome.paths[0]!, DISK);
    await known.checked(chrome.paths[0]!);
    expect(known.refused(chrome.paths[0]!, DISK)).toBeNull();
  }, 180_000);

  it("refuse an app whose signature is not there, and take one signed ad hoc whose signature checks out", async () => {
    const unsigned = app(join(home, "Applications", "Brave Browser.app"), "Brave Browser");
    const signed = app(join(home, "Applications", "Vivaldi.app"), "Vivaldi");
    // A program of the system's, signed again ad hoc as the app around it.
    copyFileSync("/usr/bin/true", signed);
    execFileSync("/usr/bin/codesign", ["--sign", "-", "--force", "--deep", join(home, "Applications", "Vivaldi.app")]);
    const known = darwinBrowsers(SYSTEM);
    expect(known.candidates(home).find(({ id }) => id === "brave")?.paths.at(-1)).toBe(unsigned);
    for (const program of [unsigned, signed]) known.refused(program, DISK);
    await Promise.all([known.checked(unsigned), known.checked(signed)]);
    expect([known.refused(unsigned, DISK), known.refused(signed, DISK)]).toEqual([NOT_SIGNED, null]);
  }, 60_000);
});

// The browsers the app knows on macOS: each one's app in /Applications or ~/Applications, its program the
// one its Info.plist names; refused on a disk image, in a temporary copy of macOS's, outside an app, or
// where its signature does not check out, which is checked once, apart, until the app changes.

import { execFileSync, spawnSync } from "node:child_process";
import { chmodSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { chosenBrowser, Disk } from "../src/browser/choose.js";
import {
  ANY_DEVELOPER, type AppTools, APPS, appOf, CHECKING, darwinBrowsers, NOT_AN_APP, NOT_SIGNED, ON_A_VOLUME, requirementOf, SYSTEM, TRANSLOCATED,
} from "../src/browser/known-darwin.js";
import type { FolderGuards } from "../src/binding/folder.js";
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
// as *signed* says, once the test lets it be said, and the requirement each was checked against.
function tools(signed: (app: string) => string | null = () => null) {
  const reads: string[] = [];
  const checks: string[] = [];
  const requirements: string[] = [];
  const waiting: Array<() => void> = [];
  const held = { on: false };
  const own: AppTools = {
    executableIn: (plist) => {
      reads.push(plist);
      const named = /<key>CFBundleExecutable<\/key>\s*<string>([^<]*)<\/string>/.exec(readFileSync(plist, "utf8"));
      if (!named) throw new Error(`${plist} names no program`);
      return named[1]!;
    },
    verify: async (at, requirement) => {
      checks.push(at);
      requirements.push(requirement);
      if (held.on) await new Promise<void>((go) => waiting.push(go));
      return signed(at);
    },
  };
  const release = () => {
    held.on = false;
    for (const go of waiting.splice(0)) go();
  };
  return { own, reads, checks, requirements, hold: () => void (held.on = true), release };
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

  // A known browser's app, by its name, is its vendor's: its identifier, under its vendor's team. Any other, picked in
  // Custom…, is one whose certificate Apple issued, which an ad hoc signature, or one by a certificate of no one's, is not.
  it("check each known browser's signature against its vendor's identifier and team, and any other's against Apple's certificates", async () => {
    const { own, requirements } = tools();
    const known = darwinBrowsers(own);
    const programs = [
      app(join(home, "Applications", "Google Chrome.app"), "Google Chrome"), app(join(home, "Applications", "Microsoft Edge.app"), "Microsoft Edge"),
      app(join(home, "Applications", "Brave Browser.app"), "Brave Browser"), app(join(home, "Applications", "Vivaldi.app"), "Vivaldi"),
      app(join(home, "Applications", "Chromium.app"), "Chromium"), app(join(home, "tools", "My Browser.app"), "My Browser"),
    ];
    for (const program of programs) known.refused(program, DISK);
    await Promise.all(programs.map((program) => known.checked(program)));
    expect(requirements).toEqual([
      'identifier "com.google.Chrome" and anchor apple generic and certificate leaf[subject.OU] = "EQHXZ8M8AV"',
      'identifier "com.microsoft.edgemac" and anchor apple generic and certificate leaf[subject.OU] = "UBF8T346G9"',
      'identifier "com.brave.Browser" and anchor apple generic and certificate leaf[subject.OU] = "KL8N8XSYF4"',
      'identifier "com.vivaldi.Vivaldi" and anchor apple generic and certificate leaf[subject.OU] = "4XF3XNRN6Y"',
      ANY_DEVELOPER, ANY_DEVELOPER,
    ]);
    expect(ANY_DEVELOPER).toBe("anchor apple generic");
    expect(requirementOf("/Applications/Google Chrome.app")).toBe(requirements[0]);
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

// The app runs the browser it drives outside every sandbox: no chat's commands may write one, nor make one where the app
// looks for one, which a chat bound to that folder could do between two looks.
describe("a chat's folder on a Mac", () => {
  let base: string;
  let guards: FolderGuards;
  beforeEach(() => {
    base = mkdtempSync(join(tmpdir(), "mac-browser-guards-"));
    guards = { home: join(base, "home"), dataDir: join(base, "data"), cacheDir: join(base, "home", ".cache", "surogate"), appDirs: [join(base, "app")] };
    for (const dir of [guards.dataDir, join(guards.home, "Applications", "Google Chrome.app", "Contents"), join(guards.home, "Applications", "Notes"),
      join(guards.home, "Applications", "Ideas"), join(base, "tools", "My Browser.app", "Contents", "MacOS"), join(base, "tools", "other"), join(base, "work")]) {
      mkdirSync(dir, { recursive: true });
    }
  });
  afterEach(() => rmSync(base, { recursive: true, force: true }));
  const checked = async (folder: string) => {
    const { checkFolder } = await importedOn("darwin", () => import("../src/binding/folder.js"));
    return checkFolder(folder, guards);
  };

  it("is refused where it is, holds or lies in a place the app finds a browser at, there or not yet", async () => {
    for (const folder of [join(guards.home, "Applications"), join(guards.home, "Applications", "Google Chrome.app"), join(guards.home, "Applications", "Google Chrome.app", "Contents")]) {
      expect(await checked(folder), folder).toMatchObject({ ok: false, missing: false, message: expect.stringContaining("outside the sandbox") });
    }
    for (const folder of [join(guards.home, "Applications", "Notes"), join(base, "work")]) expect(await checked(folder), folder).toMatchObject({ ok: true });
  });

  it("is refused where it is, holds or lies in the app of the browser picked in Custom…", async () => {
    writeFileSync(join(guards.dataDir, "browser.json"), JSON.stringify({ choice: "custom", executable: join(base, "tools", "My Browser.app", "Contents", "MacOS", "My Browser"), version: "" }));
    for (const folder of [join(base, "tools"), join(base, "tools", "My Browser.app"), join(base, "tools", "My Browser.app", "Contents", "MacOS")]) {
      expect(await checked(folder), folder).toMatchObject({ ok: false, missing: false, message: expect.stringContaining("My Browser.app") });
    }
    expect(await checked(join(base, "tools", "other"))).toMatchObject({ ok: true });
  });
});

it("keep no browser picked in Custom… that a Mac refuses", async () => {
  const choose = await importedOn("darwin", () => import("../src/browser/choose.js"));
  expect((choose.chosenBrowser as typeof chosenBrowser)({ choice: "custom", executable: "/usr/bin/chromium", version: "" }, [], DISK)).toBeNull();
  expect(await choose.unsupportedOnceChecked("/Volumes/Chromium/Chromium.app/Contents/MacOS/Chromium", DISK)).toBe(ON_A_VOLUME);
});

// codesign and plutil started as the app starts them (clean-child.ts).
describe.runIf(process.platform === "darwin")("the browsers the app knows on a Mac", () => {
  // What codesign says of *app*: whose its signature is, its own check, and the check against the vendor's requirement,
  // for a failure to name which of them refused it.
  const signatureOf = (app: string) => [["-dv", "--verbose=2", app], ["--verify", app], ["--verify", "-R", `=${requirementOf(app)}`, app]]
    .map((args) => {
      const done = spawnSync("/usr/bin/codesign", args, { encoding: "utf8" });
      return `codesign ${args.join(" ")}: ${done.status}\n${done.stderr.trim()}`;
    }).join("\n");

  for (const [id, program] of [["chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"], ["edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]] as const) {
    it(`find this Mac's ${id} in /Applications, its signature its vendor's`, async () => {
      const known = darwinBrowsers(SYSTEM);
      expect(known.candidates(home).find((browser) => browser.id === id)!.paths[0]).toBe(program);
      known.refused(program, DISK);
      await known.checked(program);
      const refused = known.refused(program, DISK);
      expect(refused, refused === null ? "" : signatureOf(appOf(program)!)).toBeNull();
    }, 180_000);
  }

  // A chat's commands can write ~/Applications where a chat is bound to it, and sign what they make there ad hoc, a
  // program of the system's signed again among it: the signature checks out, and is no one's.
  it("refuse an app whose signature is not there, a known browser's app or any other signed ad hoc, and take one of Apple's", async () => {
    const unsigned = app(join(home, "Applications", "Brave Browser.app"), "Brave Browser");
    const adHoc = (at: string, program: string) => {
      const made = app(at, program);
      // A program of the system's, signed again ad hoc as the app around it.
      copyFileSync("/usr/bin/true", made);
      execFileSync("/usr/bin/codesign", ["--sign", "-", "--force", "--deep", at]);
      return made;
    };
    const vivaldi = adHoc(join(home, "Applications", "Vivaldi.app"), "Vivaldi");
    const custom = adHoc(join(home, "tools", "My Browser.app"), "My Browser");
    const apple = "/System/Applications/Calculator.app/Contents/MacOS/Calculator";
    const known = darwinBrowsers(SYSTEM);
    expect(known.candidates(home).find(({ id }) => id === "brave")?.paths.at(-1)).toBe(unsigned);
    const programs = [unsigned, vivaldi, custom, apple];
    for (const program of programs) known.refused(program, DISK);
    await Promise.all(programs.map((program) => known.checked(program)));
    expect(programs.map((program) => known.refused(program, DISK))).toEqual([NOT_SIGNED, NOT_SIGNED, NOT_SIGNED, null]);
  }, 120_000);
});

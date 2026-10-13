// The browsers the app knows on macOS (spec, Section 5, "Choosing the browser"): each one's app in the
// Applications folder, or in the user's own, where its installer or a drag from its disk image puts it.
// Its program is the one its Info.plist names (Apple, Information Property List, CFBundleExecutable), in
// its Contents/MacOS. The app drives one only where its code signature checks out against whose it must be
// (Apple, codesign(1), --verify and -R), as Linux refuses a Snap, and only where the app stays: not
// on a mounted disk image, which goes when it is ejected, nor in the temporary copy macOS runs a downloaded app
// from until it is moved (App Translocation), which goes at the next restart. Both Applications folders can be
// the user's to write, so no chat's folder may hold a place the app looks for one at (binding/folder.ts).

import { execFile, spawnSync } from "node:child_process";
import { realpathSync, statSync } from "node:fs";
import { basename, join } from "node:path";

import { cleanly } from "../clean-child.js";
import type { BrowserId, Disk } from "./choose.js";
import type { KnownBrowser, KnownBrowsers } from "./known.js";

// Each one's app, in the order Settings lists them, and whose its signature must be: its vendor's bundle identifier,
// as its Info.plist names it (CFBundleIdentifier), and its vendor's team, which Apple's Developer ID certificate that
// signs its release names in its subject (OU), as codesign -dv shows it (TeamIdentifier): "Developer ID Application:
// Google LLC (EQHXZ8M8AV)", "... Microsoft Corporation (UBF8T346G9)", "... Brave Software, Inc. (KL8N8XSYF4)", which
// Brave's own page of its signing keys also gives (brave.com/signing-keys), and "... Vivaldi Technologies AS
// (4XF3XNRN6Y)". Chromium has no vendor who signs it. Verified: Chrome and Edge, with playwright-core 1.63.0, on the
// macOS runner's own, their signatures too; the rest are detected, unverified.
export const APPS: ReadonlyArray<{ id: BrowserId; name: string; app: string; verified: boolean; signer?: { identifier: string; team: string } }> = [
  { id: "chrome", name: "Google Chrome", app: "Google Chrome.app", verified: true, signer: { identifier: "com.google.Chrome", team: "EQHXZ8M8AV" } },
  { id: "edge", name: "Microsoft Edge", app: "Microsoft Edge.app", verified: true, signer: { identifier: "com.microsoft.edgemac", team: "UBF8T346G9" } },
  { id: "brave", name: "Brave", app: "Brave Browser.app", verified: false, signer: { identifier: "com.brave.Browser", team: "KL8N8XSYF4" } },
  { id: "vivaldi", name: "Vivaldi", app: "Vivaldi.app", verified: false, signer: { identifier: "com.vivaldi.Vivaldi", team: "4XF3XNRN6Y" } },
  { id: "chromium", name: "Chromium", app: "Chromium.app", verified: false },
];
// Where the app looks for each: the Applications folder, then the user's own.
const FOLDERS = (home: string) => ["/Applications", join(home, "Applications")];

// What an app's signature must be, in Apple's Code Signing Requirement Language (Code Signing Guide): an app by a
// known browser's name, its vendor's identifier, under a certificate Apple issued to its vendor's team; any other,
// as one picked in Custom… or Chromium, one whose certificate chain Apple issued (anchor apple generic), which a
// signature made ad hoc, or by a certificate of no one's, does not have.
export const ANY_DEVELOPER = "anchor apple generic";
export function requirementOf(app: string): string {
  const signer = APPS.find((known) => known.app === basename(app))?.signer;
  return signer ? `identifier "${signer.identifier}" and ${ANY_DEVELOPER} and certificate leaf[subject.OU] = "${signer.team}"` : ANY_DEVELOPER;
}

export const NOT_AN_APP = "a program outside an app is not supported";
export const ON_A_VOLUME = "an app on a disk image or another volume is not supported: drag it to Applications";
// Unsure: Apple documents App Translocation only in passing; the folder it copies an app to is named as macOS names it.
export const TRANSLOCATED = "an app macOS runs from a temporary copy is not supported: move it to Applications, then open it once";
export const NOT_SIGNED = "this browser's signature does not check out";
export const UNCHECKED = "this browser's signature could not be checked";
export const CHECKING = "this browser's signature is being checked";

const PLUTIL = "/usr/bin/plutil";
const CODESIGN = "/usr/bin/codesign";
const ENV = { PATH: "/usr/bin:/bin" };
const PLIST_MS = 5_000;
// codesign hashes every file the app's signature seals: a browser's are hundreds of megabytes.
const VERIFY_MS = 120_000;

/** What the app's checks of an app ask of macOS. */
export interface AppTools {
  /** The CFBundleExecutable that the Info.plist at *plist* names, as it names it. */
  executableIn(plist: string): string;
  /** Why the code signature of the app at *app* does not check out against *requirement* (requirementOf), or null where it does. */
  verify(app: string, requirement: string): Promise<string | null>;
}

const said = (error: unknown): string => (error instanceof Error ? error.message : String(error));

export const SYSTEM: AppTools = {
  // plutil(1)'s -extract with the raw format: the value alone, and a line's end.
  executableIn: (plist) => {
    const done = spawnSync(...cleanly(PLUTIL, ["-extract", "CFBundleExecutable", "raw", "-o", "-", plist]), { encoding: "utf8", env: ENV, timeout: PLIST_MS });
    if (done.status !== 0) throw new Error(`${plist} names no program: ${(done.stderr || done.error?.message || "").trim()}`);
    return done.stdout.replace(/\n$/, "");
  },
  // -R's argument, after an equals sign, is a requirement's text (codesign(1), "Specifying Requirements"). The check
  // and the requirement say the app is its vendor's code, as signed: a file changed or added since, or a signature made
  // again, fails them. Not --strict: what it adds refuses, among other things, "detritus" (a resource fork, Finder
  // information) that an install leaves on a vendor's own files, as on Edge's framework on the macOS runner, and stops
  // no app planted in its place.
  verify: (app, requirement) => new Promise((resolve) => {
    execFile(...cleanly(CODESIGN, ["--verify", "-R", `=${requirement}`, app]), { env: ENV, timeout: VERIFY_MS }, (error) => {
      resolve(error === null ? null : error.killed ? UNCHECKED : NOT_SIGNED);
    });
  }),
};

/** The app *real*, a program's whole resolved path, is the program of: <app>.app/Contents/MacOS/<file>; or null. */
export function appOf(real: string): string | null {
  return /^(\/.+\.app)\/Contents\/MacOS\/[^/]+$/.exec(real)?.[1] ?? null;
}

// What a file is now, as far as a change of it shows: an app's update replaces its files.
function stamp(path: string): string {
  try {
    const { dev, ino, size, mtimeMs } = statSync(path);
    return `${dev}:${ino}:${size}:${mtimeMs}`;
  } catch {
    return "none";
  }
}

/** The browsers the app knows on macOS, each app's program read and its signature checked by *tools*. */
export function darwinBrowsers(tools: AppTools = SYSTEM): KnownBrowsers {
  // Each app's program, read once while its Info.plist stays the same file.
  const programs = new Map<string, { stamp: string; program: string | null }>();
  // Each app's signature, checked once while its program and its sealed resources stay the same files: what it
  // was found to be, or undefined while it is checked.
  const checks = new Map<string, { stamp: string; done: Promise<void>; why: string | null | undefined }>();

  // The program of the app at *app*, by its Info.plist; null where it names none there is.
  const programOf = (app: string): string | null => {
    const plist = join(app, "Contents", "Info.plist");
    const now = stamp(plist);
    if (now === "none") return null;
    const kept = programs.get(app);
    if (kept?.stamp === now) return kept.program;
    let program: string | null = null;
    try {
      const name = tools.executableIn(plist);
      if (name !== "" && !name.includes("/") && name !== "." && name !== "..") program = join(app, "Contents", "MacOS", name);
    } catch {
      // An Info.plist that names no program: no browser there.
    }
    programs.set(app, { stamp: now, program });
    return program;
  };

  // The check of *app*'s signature, as it stands, begun where it has not been for what the app is now.
  const checkOf = (app: string, real: string) => {
    const now = `${stamp(real)}/${stamp(join(app, "Contents", "_CodeSignature", "CodeResources"))}`;
    const kept = checks.get(app);
    if (kept?.stamp === now) return kept;
    const check: { stamp: string; done: Promise<void>; why: string | null | undefined } = { stamp: now, done: Promise.resolve(), why: undefined };
    check.done = Promise.resolve().then(() => tools.verify(app, requirementOf(app))).then((why) => {
      check.why = why;
    }, (error: unknown) => {
      // No check could be run: said as it is.
      check.why = said(error);
    });
    checks.set(app, check);
    return check;
  };

  return {
    candidates: (home): readonly KnownBrowser[] => APPS.map(({ id, name, app, verified }) => ({
      id, name, verified, paths: FOLDERS(home).flatMap((folder) => programOf(join(folder, app)) ?? []),
    })),
    // Each place the app looks for a browser at, one there or not yet, and the app of the program picked.
    guarded: (home, chosen) => {
      const places = APPS.flatMap(({ app }) => FOLDERS(home).map((folder) => join(folder, app)));
      if (chosen === null) return places;
      let real = chosen;
      try {
        real = realpathSync(chosen);
      } catch {
        // Gone: guarded where it was kept.
      }
      const picked = appOf(real);
      return picked === null ? places : [...places, picked];
    },
    refused: (real: string, _disk: Disk) => {
      const app = appOf(real);
      if (app === null) return NOT_AN_APP;
      if (real.startsWith("/Volumes/")) return ON_A_VOLUME;
      if (real.includes("/AppTranslocation/")) return TRANSLOCATED;
      const check = checkOf(app, real);
      return check.why === undefined ? CHECKING : check.why;
    },
    // An app picked as a whole, as the open panel picks one, is its program; a program as it is, resolved.
    programOf: (picked: string, disk: Disk) => {
      try {
        const real = disk.realpath(picked);
        if (!real.endsWith(".app")) return real;
        const program = programOf(real);
        return program === null ? null : disk.realpath(program);
      } catch {
        return null;
      }
    },
    checked: async (real: string) => {
      const app = appOf(real);
      if (app !== null) await checkOf(app, real).done;
    },
  };
}

export const DARWIN = darwinBrowsers();

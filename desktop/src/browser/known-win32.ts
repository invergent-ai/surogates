// The browsers the app knows on Windows (spec, Section 5, "Choosing the browser"): each one's program where its
// installer puts it, for every user of the PC (under %ProgramFiles% or %ProgramFiles(x86)%) or for this user alone
// (under %LOCALAPPDATA%), then where its App Paths key says it is (Microsoft, "Application Registration"), for one
// installed elsewhere. The app drives one only where its Authenticode signature checks out, and names that
// browser's maker as its signer, as surogate-helper.exe answers (native/windows/helper, verify.rs, WinVerifyTrust),
// as macOS checks a browser's code signature; and only where it stays: not on a network volume, which can go.
// Custom… takes any program whose signature checks out, as its maker is not known.

import { statSync } from "node:fs";
import { win32 } from "node:path";

import { helper, helperSync } from "../native-helper.js";
import type { BrowserId, Disk } from "./choose.js";
import type { KnownBrowser, KnownBrowsers } from "./known.js";

export const NOT_A_PROGRAM = "a file that is not a program (.exe) is not supported";
export const ON_A_NETWORK = "a browser on a network volume is not supported: install it on this computer";
export const NOT_SIGNED = "this browser's signature does not check out";
export const UNCHECKED = "this browser's signature could not be checked";
export const CHECKING = "this browser's signature is being checked";

/**
 * Each browser's maker, by its program's file name, as its signer's subject names it (CertGetNameStringW's simple
 * display name). Chrome's and Edge's are read on the Windows runner's own. Unsure: Brave's and Vivaldi's are as their
 * makers name themselves, and are checked on a PC. Chromium's builds are not signed: its program is Chrome's name.
 */
export const PUBLISHERS: Readonly<Record<string, string>> = {
  "chrome.exe": "Google LLC",
  "msedge.exe": "Microsoft Corporation",
  "brave.exe": "Brave Software, Inc.",
  "vivaldi.exe": "Vivaldi Technologies AS",
};

// Each one's places, in the order Settings lists them, each under a folder of the environment's; and the name of the
// program its App Paths key is kept under, where its program's name is its own. Verified: Chrome and Edge, with
// playwright-core 1.63.0, on the Windows runner's own; the rest are detected, unverified.
type Folder = "ProgramFiles" | "ProgramFiles(x86)" | "LOCALAPPDATA";
const BROWSERS: ReadonlyArray<{ id: BrowserId; name: string; places: ReadonlyArray<[Folder, string]>; registered: string | null; verified: boolean }> = [
  {
    id: "chrome", name: "Google Chrome", verified: true, registered: "chrome.exe",
    places: [["ProgramFiles", "Google\\Chrome\\Application\\chrome.exe"], ["ProgramFiles(x86)", "Google\\Chrome\\Application\\chrome.exe"], ["LOCALAPPDATA", "Google\\Chrome\\Application\\chrome.exe"]],
  },
  {
    id: "edge", name: "Microsoft Edge", verified: true, registered: "msedge.exe",
    places: [["ProgramFiles(x86)", "Microsoft\\Edge\\Application\\msedge.exe"], ["ProgramFiles", "Microsoft\\Edge\\Application\\msedge.exe"]],
  },
  {
    id: "brave", name: "Brave", verified: false, registered: "brave.exe",
    places: [
      ["ProgramFiles", "BraveSoftware\\Brave-Browser\\Application\\brave.exe"], ["ProgramFiles(x86)", "BraveSoftware\\Brave-Browser\\Application\\brave.exe"],
      ["LOCALAPPDATA", "BraveSoftware\\Brave-Browser\\Application\\brave.exe"],
    ],
  },
  {
    id: "vivaldi", name: "Vivaldi", verified: false, registered: "vivaldi.exe",
    places: [["LOCALAPPDATA", "Vivaldi\\Application\\vivaldi.exe"], ["ProgramFiles", "Vivaldi\\Application\\vivaldi.exe"]],
  },
  { id: "chromium", name: "Chromium", verified: false, registered: null, places: [["LOCALAPPDATA", "Chromium\\Application\\chrome.exe"]] },
];

// How long the App Paths keys' answer is said again before they are read anew: Settings asks at each of its looks.
const KEPT_MS = 30_000;
// A browser's program is hundreds of megabytes; its signature is checked against all of them.
const VERIFY_MS = 120_000;

/** What the app's checks of a program ask of Windows. */
export interface ProgramTools {
  /** The paths the App Paths keys give the program named *name*, the machine's then the user's. */
  appPaths(name: string): string[];
  /** What WinVerifyTrust says of the program at *path*. */
  verify(path: string): Promise<{ trusted: boolean; signer: string | null; remote: boolean }>;
  /** What the file at *path* is now, as far as a change of it shows. */
  stamp(path: string): string;
  /** The program at *path*'s product version, from its own version resource, or null where it has none. */
  version(path: string): Promise<string | null>;
}

/** What a file is now, as far as a change of it shows: an update replaces its program. */
export function fileStamp(path: string): string {
  try {
    const stats = statSync(path);
    return `${stats.size}:${stats.mtimeMs}:${stats.birthtimeMs}`;
  } catch {
    return "none";
  }
}

// What the helper's verify says, of its own shape alone.
function verdictIn(text: string): { trusted: boolean; signer: string | null; remote: boolean } {
  const said = JSON.parse(text) as Partial<{ trusted: unknown; signer: unknown; remote: unknown }>;
  if (typeof said.trusted !== "boolean" || typeof said.remote !== "boolean" || !(said.signer === null || typeof said.signer === "string")) {
    throw new Error("surogate-helper.exe said no verdict");
  }
  return { trusted: said.trusted, signer: said.signer, remote: said.remote };
}

export const SYSTEM: ProgramTools = {
  appPaths: (name) => helperSync(["app-path", name]).split(/\r?\n/).filter((path) => path !== ""),
  verify: async (path) => verdictIn(await Promise.race([
    helper(["verify", path]),
    new Promise<never>((_resolve, reject) => setTimeout(() => reject(new Error("no answer in time")), VERIFY_MS).unref()),
  ])),
  stamp: fileStamp,
  version: (path) => helper(["version", path]).then((said) => said.trim() || null, () => null),
};

// A whole path on a drive, as the environment names a folder: neither relative nor on a network share.
const ON_A_DRIVE = /^[A-Za-z]:[\\/]/;
const isProgram = (path: string): boolean => win32.extname(path).toLowerCase() === ".exe";

/** The browsers the app knows on Windows, for the user whose environment is *env*, each program checked by *tools*. */
export function windowsBrowsers(tools: ProgramTools = SYSTEM, env: NodeJS.ProcessEnv = process.env): KnownBrowsers {
  let registered: { at: number; paths: Map<string, string[]> } | null = null;
  // Each program's signature, checked once while the program stays the same file: what it was found to be, or
  // undefined while it is checked.
  const checks = new Map<string, { stamp: string; done: Promise<void>; why: string | null | undefined }>();

  const folder = (named: Folder, home: string): string | null => {
    const set = env[named];
    if (set && ON_A_DRIVE.test(set)) return win32.normalize(set);
    // The user's own, where the environment names none: it is under their profile.
    return named === "LOCALAPPDATA" ? win32.join(home, "AppData", "Local") : null;
  };

  // The App Paths keys' answers, read once in KEPT_MS; a key that cannot be read gives no path.
  const appPaths = (): Map<string, string[]> => {
    if (registered === null || Date.now() - registered.at >= KEPT_MS) {
      const paths = new Map<string, string[]>();
      for (const { registered: name } of BROWSERS) {
        if (name === null) continue;
        try {
          paths.set(name, tools.appPaths(name).filter((path) => ON_A_DRIVE.test(path)));
        } catch {
          paths.set(name, []);
        }
      }
      registered = { at: Date.now(), paths };
    }
    return registered.paths;
  };

  // The check of *real*'s signature, as it stands, begun where it has not been for what the program is now.
  const checkOf = (real: string) => {
    const now = tools.stamp(real);
    const kept = checks.get(real);
    if (kept?.stamp === now) return kept;
    const check: { stamp: string; done: Promise<void>; why: string | null | undefined } = { stamp: now, done: Promise.resolve(), why: undefined };
    const maker = PUBLISHERS[win32.basename(real).toLowerCase()];
    check.done = Promise.resolve().then(() => tools.verify(real)).then(({ trusted, signer, remote }) => {
      if (remote) check.why = ON_A_NETWORK;
      else if (!trusted || signer === null || (maker !== undefined && signer !== maker)) check.why = NOT_SIGNED;
      else check.why = null;
    }, (error: unknown) => {
      // No check could be run: said so, until the program changes.
      check.why = UNCHECKED;
    });
    checks.set(real, check);
    return check;
  };

  return {
    candidates: (home): readonly KnownBrowser[] => BROWSERS.map(({ id, name, places, registered: named, verified }) => {
      const paths: string[] = [];
      for (const [under, path] of places) {
        const root = folder(under, home);
        if (root !== null) paths.push(win32.join(root, path));
      }
      for (const path of named === null ? [] : appPaths().get(named) ?? []) {
        if (!paths.some((known) => known.toLowerCase() === path.toLowerCase())) paths.push(path);
      }
      return { id, name, verified, paths };
    }),
    refused: (real: string, _disk: Disk) => {
      if (!isProgram(real)) return NOT_A_PROGRAM;
      if (!ON_A_DRIVE.test(real)) return ON_A_NETWORK;
      const check = checkOf(real);
      return check.why === undefined ? CHECKING : check.why;
    },
    // A program as it is, resolved: null for one gone since it was picked.
    programOf: (picked: string, disk: Disk) => {
      try {
        return disk.realpath(picked);
      } catch {
        return null;
      }
    },
    checked: async (real: string) => {
      if (isProgram(real) && ON_A_DRIVE.test(real)) await checkOf(real).done;
    },
    // Windows names a program by its .exe, as the open dialog's filter does (Electron, dialog, filters).
    pickFilters: [{ name: "Programs", extensions: ["exe"] }],
    // A browser on Windows starts a window of its own for --version, and prints nothing: its version is its file's.
    version: (executable) => tools.version(executable),
  };
}

export const WIN32 = windowsBrowsers();

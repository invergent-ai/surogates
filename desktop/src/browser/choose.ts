// Which browser the agent drives on this computer (spec, Section 5, "Choosing the browser"):
// one installed from its own package, as Settings → Browser chose it, for every agent. The
// choice is a program's path, so only this computer's user makes it, never the web page.
// Snap and Flatpak builds are not supported: their confinement breaks the profile folder and
// the pipe. Ubuntu ships Chromium only as a Snap, and its chromium-browser package as a script
// that runs it.

import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { accessSync, closeSync, constants, openSync, readSync, realpathSync, statSync } from "node:fs";
import { join } from "node:path";

import { readState, writeState } from "../shell/state-file.js";

export type BrowserId = "chrome" | "edge" | "brave" | "vivaldi" | "chromium";

// Where each one's package puts it, its own program first. Verified: Chrome and Edge from
// their .deb, with playwright-core 1.63.0; the rest are detected, unverified.
export const KNOWN: ReadonlyArray<{ id: BrowserId; name: string; paths: readonly string[]; verified: boolean }> = [
  { id: "chrome", name: "Google Chrome", paths: ["/opt/google/chrome/chrome", "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"], verified: true },
  { id: "edge", name: "Microsoft Edge", paths: ["/opt/microsoft/msedge/msedge", "/usr/bin/microsoft-edge-stable", "/usr/bin/microsoft-edge"], verified: true },
  { id: "brave", name: "Brave", paths: ["/opt/brave.com/brave/brave", "/usr/bin/brave-browser"], verified: false },
  { id: "vivaldi", name: "Vivaldi", paths: ["/opt/vivaldi/vivaldi", "/usr/bin/vivaldi-stable", "/usr/bin/vivaldi"], verified: false },
  // The Snap first, so it is looked at wherever another path leads; then Debian's own program.
  { id: "chromium", name: "Chromium", paths: ["/snap/bin/chromium", "/usr/lib/chromium/chromium", "/usr/bin/chromium", "/usr/bin/chromium-browser"], verified: false },
];

export interface Found {
  id: BrowserId | "custom";
  name: string;
  executable: string; // resolved
  verified: boolean;
  unsupported: string | null; // why it cannot be the agent's
}

export type BrowserChoice = { choice: "auto" } | { choice: BrowserId } | { choice: "custom"; executable: string; version: string };

export interface Disk {
  realpath(path: string): string; // throws for a path that is not there
  executable(path: string): boolean; // a regular file this user may run
  head(path: string): string; // its first HEAD_BYTES, as Latin-1; empty when unreadable
}

const HEAD_BYTES = 4_096;

const DISK: Disk = {
  realpath: (path) => realpathSync(path),
  executable: (path) => {
    try {
      accessSync(path, constants.X_OK);
      return statSync(path).isFile();
    } catch {
      return false;
    }
  },
  head: (path) => {
    let fd: number | undefined;
    try {
      fd = openSync(path, "r");
      const buffer = Buffer.alloc(HEAD_BYTES);
      return buffer.subarray(0, readSync(fd, buffer, 0, HEAD_BYTES, 0)).toString("latin1");
    } catch {
      return "";
    } finally {
      if (fd !== undefined) closeSync(fd);
    }
  },
};

/** Why the program at *real*, resolved, cannot be the agent's browser: a Snap or a Flatpak build. Null when it can. */
export function confined(real: string): string | null {
  if (real === "/usr/bin/snap" || real.startsWith("/snap/")) return "the Snap build is not supported";
  if (real.includes("/flatpak/")) return "a Flatpak build is not supported";
  return null;
}

/** Why the program at *real*, resolved, cannot be the agent's: confined(), or a script that runs a Snap. Null when it can. */
export function unsupportedAt(real: string, disk: Disk = DISK): string | null {
  const head = disk.head(real);
  return confined(real) ?? (head.startsWith("#!") && head.includes("/snap/") ? "the Snap build is not supported" : null);
}

/** The known browsers installed here, each once: at the first of its paths it can be, else the first found. */
export function findBrowsers(disk: Disk = DISK): Found[] {
  const found: Found[] = [];
  for (const known of KNOWN) {
    const at: Found[] = [];
    for (const path of known.paths) {
      let real: string;
      try {
        real = disk.realpath(path);
      } catch {
        continue;
      }
      if (!disk.executable(real)) continue;
      at.push({ id: known.id, name: known.name, executable: real, verified: known.verified, unsupported: unsupportedAt(real, disk) });
    }
    const one = at.find((browser) => browser.unsupported === null) ?? at[0];
    if (one) found.push(one);
  }
  return found;
}

/**
 * The browser the agent's next launch uses: under Automatic, the first supported one found;
 * otherwise the one chosen, while it is still installed and supported. Null: none, and the
 * browser tools say so.
 */
export function chosenBrowser(choice: BrowserChoice, found: Found[], disk: Disk = DISK): Found | null {
  if (choice.choice === "auto") return found.find((browser) => browser.unsupported === null) ?? null;
  if (choice.choice === "custom") {
    let real: string;
    try {
      real = disk.realpath(choice.executable);
    } catch {
      return null;
    }
    if (!disk.executable(real) || unsupportedAt(real, disk) !== null) return null;
    return { id: "custom", name: "Your browser", executable: real, verified: false, unsupported: null };
  }
  const browser = found.find((one) => one.id === choice.choice);
  return browser?.unsupported === null ? browser : null;
}

const CHOICE_IDS = new Set<string>(["auto", ...KNOWN.map(({ id }) => id)]);

/** Settings → Browser's choice, kept beside the app's other settings: Automatic until the user picks another. */
export class BrowserSetting {
  constructor(private readonly path: string) {}

  get(): BrowserChoice {
    const saved = readState<Record<string, unknown>>(this.path, {});
    if (saved.choice === "custom" && typeof saved.executable === "string" && saved.executable.startsWith("/")) {
      return { choice: "custom", executable: saved.executable, version: typeof saved.version === "string" ? saved.version : "" };
    }
    return typeof saved.choice === "string" && CHOICE_IDS.has(saved.choice) ? ({ choice: saved.choice } as BrowserChoice) : { choice: "auto" };
  }

  set(choice: BrowserChoice): void {
    writeState(this.path, choice);
  }
}

export interface ChoiceRow {
  value: string; // "auto", a browser's id, "custom" for the one kept, "pick" for Custom…
  label: string;
  disabled: boolean;
}

// A browser as Settings names it: its name, its version, and whether it is verified or why not supported.
function labelOf(browser: Found, version: string | undefined): string {
  const named = version ? `${browser.name} ${version}` : browser.name;
  if (browser.unsupported) return `${named}: ${browser.unsupported}`;
  return browser.verified ? named : `${named} (not verified)`;
}

/** What Settings → Browser offers, in its order: Automatic, each one found, the custom one kept, and Custom…. */
export function choiceRows(choice: BrowserChoice, found: Found[], versions: ReadonlyMap<string, string>): ChoiceRow[] {
  const auto = chosenBrowser({ choice: "auto" }, found);
  return [
    { value: "auto", label: auto ? `Automatic (${labelOf(auto, versions.get(auto.executable))})` : "Automatic (none found)", disabled: false },
    ...found.map((browser) => ({ value: browser.id, label: labelOf(browser, versions.get(browser.executable)), disabled: browser.unsupported !== null })),
    ...(choice.choice === "custom" ? [{ value: "custom", label: `${choice.executable}${choice.version ? ` ${choice.version}` : ""}`, disabled: false }] : []),
    { value: "pick", label: "Custom…", disabled: false },
  ];
}

// Each browser's version as it said it, until its program changes (an update).
const versions = new Map<string, { mtime: number; version: string | null }>();

/**
 * What the browser says its version is, for Settings: this runs the program, so only for one found
 * at its package's path, and once until the program changes.
 */
export async function browserVersion(executable: string): Promise<string | null> {
  let mtime: number;
  try {
    mtime = statSync(executable).mtimeMs;
  } catch {
    return null;
  }
  const known = versions.get(executable);
  if (known?.mtime === mtime) return known.version;
  const version = await new Promise<string | null>((resolve) => {
    execFile(executable, ["--version"], { timeout: 5_000 }, (error, stdout) => {
      resolve(error ? null : (/\d+(\.\d+)+/.exec(stdout)?.[0] ?? null));
    });
  });
  versions.set(executable, { mtime, version });
  return version;
}

// An agent identity: the server's origin, the org, the agent and the user.
export interface BrowserIdentity {
  origin: string;
  orgId: string;
  agentId: string;
  userId: string;
}

/** The folder that holds every profile of an identity's browser, under the app's own state. */
export function profilesOf(dataDir: string, who: BrowserIdentity): string {
  const hash = createHash("sha256").update([who.origin, who.orgId, who.agentId, who.userId].join("\n")).digest("hex").slice(0, 32);
  return join(dataDir, "browser-profiles", hash);
}

/** The identity's profile for *browser*: one per browser, so switching back finds its sign-ins. */
export function profileOf(dataDir: string, who: BrowserIdentity, browser: Found): string {
  const which = browser.id === "custom" ? `custom-${createHash("sha256").update(browser.executable).digest("hex").slice(0, 12)}` : browser.id;
  return join(profilesOf(dataDir, who), which);
}

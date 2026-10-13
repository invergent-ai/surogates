// The browsers the app knows on Linux (spec, Section 5, "Choosing the browser"): where each one's
// package puts it, and which builds it does not drive. Snap and Flatpak builds are not supported:
// their confinement breaks the profile folder and the pipe. Ubuntu ships Chromium only as a Snap,
// and its chromium-browser package as a script that runs it.

import type { Disk } from "./choose.js";
import type { KnownBrowser, KnownBrowsers } from "./known.js";

// Where each one's package puts it, its own program first. Verified: Chrome and Edge from
// their .deb, with playwright-core 1.63.0; the rest are detected, unverified.
export const KNOWN: readonly KnownBrowser[] = [
  { id: "chrome", name: "Google Chrome", paths: ["/opt/google/chrome/chrome", "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"], verified: true },
  { id: "edge", name: "Microsoft Edge", paths: ["/opt/microsoft/msedge/msedge", "/usr/bin/microsoft-edge-stable", "/usr/bin/microsoft-edge"], verified: true },
  { id: "brave", name: "Brave", paths: ["/opt/brave.com/brave/brave", "/usr/bin/brave-browser"], verified: false },
  { id: "vivaldi", name: "Vivaldi", paths: ["/opt/vivaldi/vivaldi", "/usr/bin/vivaldi-stable", "/usr/bin/vivaldi"], verified: false },
  // The Snap first, so it is looked at wherever another path leads; then Debian's own program.
  { id: "chromium", name: "Chromium", paths: ["/snap/bin/chromium", "/usr/lib/chromium/chromium", "/usr/bin/chromium", "/usr/bin/chromium-browser"], verified: false },
];

/** Why the program at *real*, resolved, cannot be the agent's browser: a Snap or a Flatpak build. Null when it can. */
export function confined(real: string): string | null {
  if (real === "/usr/bin/snap" || real.startsWith("/snap/")) return "the Snap build is not supported";
  if (real.includes("/flatpak/")) return "a Flatpak build is not supported";
  return null;
}

export const LINUX: KnownBrowsers = {
  candidates: () => KNOWN,
  // confined(), or a script that runs a Snap.
  refused: (real: string, disk: Disk) => {
    const head = disk.head(real);
    return confined(real) ?? (head.startsWith("#!") && head.includes("/snap/") ? "the Snap build is not supported" : null);
  },
  // The file itself, resolved: null for one gone since it was picked, or a link that leads nowhere.
  programOf: (picked: string, disk: Disk) => {
    try {
      return disk.realpath(picked);
    } catch {
      return null;
    }
  },
  // Every rule of Linux's is said at once.
  checked: async () => {},
  // Its known browsers are the system's packages', which the user cannot write.
  guarded: () => [],
};

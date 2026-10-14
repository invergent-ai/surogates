// The platform and architecture a release of the app is built for, its target, and the names its
// files have on the release bucket, under <base>/desktop/ (release/publish.sh, target_files, names
// them the same). Linux's are the names every release before macOS's had; macOS's and Windows's are
// in folders of their own beside them, so that no target's latest.json names another's release.
// And the one lookup by which each part of the app picks its side for the OS it runs on.
// Electron-free.

export type Target = "linux-x64" | "darwin-arm64" | "win32-x64";

export interface TargetFiles {
  platform: "linux" | "darwin" | "win32";
  arch: "x64" | "arm64";
  /** The release's payload, by its name alone: a tarball, or Windows's MSI. */
  archive(version: string): string;
  /** The folder its payload, its manifest and the manifest's signature are sent to, and kept in. */
  releaseDir(version: string): string;
  /** The manifest of the newest release, which every app and install reads first. */
  latest: string;
}

export const TARGETS: Readonly<Record<Target, TargetFiles>> = {
  "linux-x64": {
    platform: "linux",
    arch: "x64",
    archive: (version) => `surogate-desktop-${version}-linux-x64.tar.gz`,
    releaseDir: (version) => `releases/${version}`,
    latest: "latest.json",
  },
  "darwin-arm64": {
    platform: "darwin",
    arch: "arm64",
    archive: (version) => `surogate-desktop-${version}-darwin-arm64.tar.gz`,
    releaseDir: (version) => `releases/${version}/darwin-arm64`,
    latest: "darwin-arm64/latest.json",
  },
  "win32-x64": {
    platform: "win32",
    arch: "x64",
    archive: (version) => `surogate-desktop-${version}-win32-x64.msi`,
    releaseDir: (version) => `releases/${version}/win32-x64`,
    latest: "win32-x64/latest.json",
  },
};

/**
 * A part's row for *platform*, this computer's OS by default, or null where it has none: one lookup for every part
 * that differs by OS, so that an OS is a row of each, and a part is picked once, as its module is imported.
 */
export function forThisOs<T>(rows: Partial<Record<NodeJS.Platform, T>>, platform: NodeJS.Platform = process.platform): T | null {
  return rows[platform] ?? null;
}

/** Each OS by the name a person knows it by, where that is not the name Node gives it (files/os-names.ts keeps a copy). */
export const OS_NAMES: Readonly<Partial<Record<NodeJS.Platform, string>>> = { darwin: "macOS", win32: "Windows" };

// The OS this app runs on, by the name a person knows it by: read once, as each part picks its side.
const OS = forThisOs(OS_NAMES) ?? process.platform;

/** What a part of the app that has no row for this OS yet says here: that *what* is not built for this OS yet. */
export const notBuiltWords = (what: string): string => `${what} is not built for ${OS} yet`;

/** What such a part does there: it refuses, in those words. */
export function notBuilt(what: string): never {
  throw new Error(notBuiltWords(what));
}

/** This app's target, or null on a platform and arch the app is not built for. */
export function targetOf(platform: NodeJS.Platform = process.platform, arch: NodeJS.Architecture = process.arch): Target | null {
  const found = (Object.keys(TARGETS) as Target[]).find((target) => TARGETS[target].platform === platform && TARGETS[target].arch === arch);
  return found ?? null;
}

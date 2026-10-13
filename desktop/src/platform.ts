// The platform and architecture a release of the app is built for, its target, and the names its
// files have on the release bucket, under <base>/desktop/ (release/publish.sh, target_files, names
// them the same). Linux's are the names every release before macOS's had; macOS's are in folders of
// their own beside them, so that neither's latest.json names the other's release.
// And the one lookup by which each part of the app picks its side for the OS it runs on.
// Electron-free.

export type Target = "linux-x64" | "darwin-arm64";

export interface TargetFiles {
  platform: "linux" | "darwin";
  arch: "x64" | "arm64";
  /** The release's tarball, by its name alone. */
  tarball(version: string): string;
  /** The folder its tarball, its manifest and the manifest's signature are sent to, and kept in. */
  releaseDir(version: string): string;
  /** The manifest of the newest release, which every app and install reads first. */
  latest: string;
}

export const TARGETS: Readonly<Record<Target, TargetFiles>> = {
  "linux-x64": {
    platform: "linux",
    arch: "x64",
    tarball: (version) => `surogate-desktop-${version}-linux-x64.tar.gz`,
    releaseDir: (version) => `releases/${version}`,
    latest: "latest.json",
  },
  "darwin-arm64": {
    platform: "darwin",
    arch: "arm64",
    tarball: (version) => `surogate-desktop-${version}-darwin-arm64.tar.gz`,
    releaseDir: (version) => `releases/${version}/darwin-arm64`,
    latest: "darwin-arm64/latest.json",
  },
};

/**
 * A part's row for *platform*, this computer's OS by default, or null where it has none: one lookup for every part
 * that differs by OS, so that an OS is a row of each, and a part is picked once, as its module is imported.
 */
export function forThisOs<T>(rows: Partial<Record<NodeJS.Platform, T>>, platform: NodeJS.Platform = process.platform): T | null {
  return rows[platform] ?? null;
}

// The OS this app runs on, by the name a person knows it by: read once, as each part picks its side.
const OS = forThisOs({ darwin: "macOS" }) ?? process.platform;

/** What a part of the app that has only Linux's side so far says on another OS: that *what* is not built for that OS yet. */
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

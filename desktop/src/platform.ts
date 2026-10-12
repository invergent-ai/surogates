// The platform and architecture a release of the app is built for, its target, and the names its
// files have on the release bucket, under <base>/desktop/ (release/publish.sh, target_files, names
// them the same). Linux's are the names every release before macOS's had; macOS's are in folders of
// their own beside them, so that neither's latest.json names the other's release.
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

/** This app's target, or null on a platform and arch the app is not built for. */
export function targetOf(platform: NodeJS.Platform = process.platform, arch: NodeJS.Architecture = process.arch): Target | null {
  const found = (Object.keys(TARGETS) as Target[]).find((target) => TARGETS[target].platform === platform && TARGETS[target].arch === arch);
  return found ?? null;
}

import { configDefaults, defineConfig } from "vitest/config";

// A FUSE mount that comes or goes while bwrap binds / fails that sandbox's start (its remount,
// EINVAL), and srt's file hosts bind / whole: the files that mount one run alone, after the rest.
const MOUNTING = ["test/vm-stalled-folder.test.ts", "test/vm/guest-places.test.ts"];

// The files that pass on macOS, which the macOS workflow runs (.github/workflows/desktop-macos.yml): each
// part of the app built for macOS adds its own. A project on macOS alone: on Linux each is the unit project's.
export const MACOS = [
  "test/platform.test.ts", "test/backoff.test.ts", "test/egress.test.ts", "test/flags.test.ts", "test/guest-control.test.ts", "test/output.test.ts",
  "test/protocol.test.ts", "test/vm-manifest.test.ts",
  // The file host under Seatbelt: clean spawns, and what needs nothing built. Its other suites need fsat, rg and a
  // node at bin/node, and take long: the workflow's file-host job runs them, each on its own.
  "test/clean-child.test.ts", "test/agent-disk-imports.test.ts", "test/policy-darwin.test.ts",
  // The VM backend: what starts no program, and surogate-vm's stand-in started as the app starts it, on the app's built code.
  "test/vm-darwin.test.ts", "test/vm-darwin-boot.test.ts",
  // The shell and the browser on macOS.
  "test/shell-places-darwin.test.ts", "test/computer-name-darwin.test.ts", "test/backups-darwin.test.ts", "test/folders-held-darwin.test.ts", "test/mac-helper.test.ts",
  "test/login-item-darwin.test.ts", "test/deep-link-darwin.test.ts", "test/credentials-darwin.test.ts",
  "test/menus-darwin.test.ts", "test/main-window-darwin.test.ts", "test/quick-entry-darwin.test.ts",
  "test/ca-source-darwin.test.ts",
  "test/browser-known-darwin.test.ts", "test/browser-processes-darwin.test.ts", "test/browser-host-darwin.test.ts",
];

// The files that pass on Windows, which the Windows workflow runs (.github/workflows/desktop-windows.yml): each part of
// the app built for Windows adds its own. A project on Windows alone: on Linux each is the unit project's.
export const WINDOWS = [
  "test/platform.test.ts", "test/backoff.test.ts", "test/egress.test.ts", "test/guest-control.test.ts", "test/output.test.ts", "test/protocol.test.ts",
  "test/vm-manifest.test.ts",
  // The seams each part of the app picks its OS by, which name Windows's rows.
  "test/platform-seams.test.ts", "test/session-paths.test.ts", "test/native-helper.test.ts", "test/windows-project.test.ts",
  // The file host on Windows: what needs nothing built. Its other suites need ntat, rg and dist/: the workflow's
  // file-host job runs them, each on its own.
  "test/names-win32.test.ts", "test/srt-win32.test.ts", "test/glob-win32.test.ts",
  // The shell and the browser on Windows.
  "test/session-path-win32.test.ts", "test/shell-places-win32.test.ts", "test/host-user.test.ts", "test/computer-name-win32.test.ts",
  "test/credentials-win32.test.ts", "test/backups.test.ts",
  "test/login-item-win32.test.ts", "test/deep-link-win32.test.ts", "test/app-identity-win32.test.ts", "test/notifications-win32.test.ts",
  "test/menus-win32.test.ts", "test/main-window-win32.test.ts", "test/quick-entry-win32.test.ts",
  "test/ca-source-win32.test.ts", "test/tools-look.test.ts",
  "test/browser-known-win32.test.ts", "test/browser-processes-win32.test.ts", "test/browser-host-win32.test.ts",
  "test/folders-held-win32.test.ts",
  // The native programs' one workspace, and the one script that builds the Windows app.
  "test/windows-native.test.ts",
  // The guest's disks as Windows opens them, fixed VHDs: one mounted read-only.
  "test/vhd-win32.test.ts",
  // Every folder and file the app shows its person, by its Windows path.
  "test/shown-paths.test.ts",
];

// Every git the tests run themselves reads none of the user's or the system's config, so it
// never signs with the user's key or reaches their gpg-agent, and commits as the tests.
const GIT = {
  GIT_CONFIG_GLOBAL: "/dev/null",
  GIT_CONFIG_NOSYSTEM: "1",
  GIT_AUTHOR_NAME: "Surogate tests",
  GIT_AUTHOR_EMAIL: "tests@surogate.invalid",
  GIT_COMMITTER_NAME: "Surogate tests",
  GIT_COMMITTER_EMAIL: "tests@surogate.invalid",
};

export default defineConfig({
  test: {
    env: GIT,
    environment: "node",
    testTimeout: 10_000,
    globalSetup: ["test/srt-tmp.ts", "test/isolated.ts"],
    // The VM suite on the stand-in for Hyper-V's devices, where a run asks for it (test/vm/hyperv/standin.ts).
    setupFiles: process.env.SUROGATE_VM_STANDIN ? ["test/vm/hyperv/setup.ts"] : [],
    projects: [
      { extends: true, test: { name: "unit", include: ["test/**/*.test.ts"], exclude: [...configDefaults.exclude, ...MOUNTING] } },
      { extends: true, test: { name: "mounting", include: MOUNTING, sequence: { groupOrder: 1 } } },
      ...(process.platform === "darwin" ? [{ extends: true as const, test: { name: "macos", include: MACOS } }] : []),
      ...(process.platform === "win32" ? [{ extends: true as const, test: { name: "windows", include: WINDOWS } }] : []),
    ],
  },
});

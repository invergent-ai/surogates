import { configDefaults, defineConfig } from "vitest/config";

// A FUSE mount that comes or goes while bwrap binds / fails that sandbox's start (its remount,
// EINVAL), and srt's file hosts bind / whole: the files that mount one run alone, after the rest.
const MOUNTING = ["test/vm-stalled-folder.test.ts"];

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
    globalSetup: ["test/srt-tmp.ts"],
    projects: [
      { extends: true, test: { name: "unit", include: ["test/**/*.test.ts"], exclude: [...configDefaults.exclude, ...MOUNTING] } },
      { extends: true, test: { name: "mounting", include: MOUNTING, sequence: { groupOrder: 1 } } },
    ],
  },
});

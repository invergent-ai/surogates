import { configDefaults, defineConfig } from "vitest/config";

// A FUSE mount that comes or goes while bwrap binds / fails that sandbox's start (its remount,
// EINVAL), and srt's file hosts bind / whole: the files that mount one run alone, after the rest.
const MOUNTING = ["test/vm-stalled-folder.test.ts"];

export default defineConfig({
  test: {
    environment: "node",
    testTimeout: 10_000,
    globalSetup: ["test/srt-tmp.ts"],
    projects: [
      { extends: true, test: { name: "unit", include: ["test/**/*.test.ts"], exclude: [...configDefaults.exclude, ...MOUNTING] } },
      { extends: true, test: { name: "mounting", include: MOUNTING, sequence: { groupOrder: 1 } } },
    ],
  },
});

import { defineConfig } from "vitest/config";

// The shell end to end: Electron under Playwright, one app at a time.
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
    include: ["test/e2e/**/*.e2e.ts"],
    environment: "node",
    testTimeout: 60_000,
    hookTimeout: 60_000,
    // What a poll waits for takes seconds on a machine that runs other suites beside this one: a page's
    // load, a window, a link's handshake, an OAuth round trip, Settings' first state, DevTools. A poll
    // asserts the same value however long it waits; it only fails later.
    expect: { poll: { timeout: 10_000 } },
    fileParallelism: false,
    globalSetup: ["test/srt-tmp.ts"],
  },
});

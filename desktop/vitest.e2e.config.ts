import { defineConfig } from "vitest/config";

// The shell end to end: Electron under Playwright, one app at a time.
export default defineConfig({
  test: {
    include: ["test/e2e/**/*.e2e.ts"],
    environment: "node",
    testTimeout: 60_000,
    hookTimeout: 60_000,
    fileParallelism: false,
    globalSetup: ["test/srt-tmp.ts"],
  },
});

import { defineConfig } from "vitest/config";

export default defineConfig({
  // The integration files share one fixture port: one file at a time.
  test: { environment: "node", include: ["test/**/*.test.ts"], fileParallelism: false, testTimeout: 120_000, hookTimeout: 120_000 },
});

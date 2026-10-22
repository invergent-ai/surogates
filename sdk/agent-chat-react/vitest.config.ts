import { defineConfig } from "vitest/config";
import { resolve } from "node:path";

export default defineConfig({
  resolve: {
    alias: {
      "@": resolve(__dirname, "src"),
      "@invergent/ui-sdk": resolve(__dirname, "../ui-sdk/src"),
    },
  },
  test: {
    environment: "happy-dom",
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
    // Half this computer's threads; vitest's own floor of workers would otherwise ask for more than that.
    maxWorkers: "50%",
    minWorkers: 1,
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/**/*.{ts,tsx}"],
    },
  },
});

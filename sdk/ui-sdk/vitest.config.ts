import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "happy-dom",
    include: ["tests/**/*.test.tsx"],
    // Half this computer's threads; vitest's own floor of workers would otherwise ask for more than that.
    maxWorkers: "50%",
    minWorkers: 1,
  },
});

import { defineConfig } from "vitest/config";

// The measurements of the macOS VM, on a Mac alone (prepare.sh, harness.ts): each file of this folder
// is one, run by name, one at a time, each with an hour at most.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts <name>
export default defineConfig({
  test: {
    environment: "node",
    include: ["test/vm/darwin/measure/*.ts"],
    exclude: ["test/vm/darwin/measure/harness.ts", "test/vm/darwin/measure/vitest.config.ts"],
    testTimeout: 3_600_000,
    hookTimeout: 600_000,
    fileParallelism: false,
    reporters: ["verbose"],
  },
});

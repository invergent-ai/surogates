import { defineConfig } from "vitest/config";

// The measurements of the Windows VM, on a PC alone (harness.ts): each file of this folder is one, run by name, one
// at a time, each with an hour at most. Their order on the test day is the share-identity security stop first (identity), then
// boot, ports, disks, owners, readonly, shares, coherence, mmap, folders, lifetime, memory, beside, defender,
// requirements; sleep is by hand.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts identity
export default defineConfig({
  test: {
    environment: "node",
    include: ["test/vm/win32/measure/*.ts"],
    exclude: ["test/vm/win32/measure/harness.ts", "test/vm/win32/measure/vitest.config.ts"],
    testTimeout: 3_600_000,
    hookTimeout: 600_000,
    fileParallelism: false,
    reporters: ["verbose"],
  },
});

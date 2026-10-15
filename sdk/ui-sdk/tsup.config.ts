import { readdirSync } from "node:fs";
import { defineConfig } from "tsup";

// One entry per component, so a host imports exactly what it uses
// ("@invergent/ui-sdk/button") and the shared pieces split into chunks.
const components = readdirSync("src").filter((f) => f.endsWith(".tsx"));

export default defineConfig({
  entry: [...components.map((f) => `src/${f}`), "src/lib/utils.ts"],
  format: ["esm", "cjs"],
  dts: true,
  sourcemap: true,
  clean: true,
  treeshake: true,
  splitting: true,
  target: "es2020",
  outDir: "dist",
  external: ["react", "react-dom", "react/jsx-runtime"],
});

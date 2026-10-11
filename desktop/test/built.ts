// What a test runs of dist/ (npm run build first), checked against the sources it was built from: a test that
// runs an older build says so, and which file, rather than fail, or pass, as that build does.

import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
const DIST = join(DESKTOP, "dist");
const SRC = join(DESKTOP, "src");
// Each module of the package's own that a built one imports, at its top or as it runs.
const IMPORTS = /(?:\bfrom\s*|\bimport\s*\(?\s*)["'](\.{1,2}\/[^"']+\.js)["']/g;

/**
 * *module*, a file of dist/, once it and every module of dist/ it imports are there and no older than their
 * sources. Throws otherwise, naming each that is not, and what to run.
 */
export function built(module: string): string {
  const stale: string[] = [];
  const seen = new Set<string>();
  const visit = (file: string) => {
    if (seen.has(file)) return;
    seen.add(file);
    if (!existsSync(file)) {
      stale.push(`${relative(DESKTOP, file)} is not built`);
      return;
    }
    const source = join(SRC, relative(DIST, file)).replace(/\.js$/, ".ts");
    if (existsSync(source) && statSync(source).mtimeMs > statSync(file).mtimeMs) stale.push(`${relative(DESKTOP, file)} is older than ${relative(DESKTOP, source)}`);
    for (const [, imported] of readFileSync(file, "utf8").matchAll(IMPORTS)) visit(resolve(dirname(file), imported!));
  };
  visit(module);
  if (stale.length > 0) throw new Error(`${stale.join("; ")}: run npm run build first`);
  return module;
}

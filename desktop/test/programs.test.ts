// The build's two programs: the app's Node processes, typed without the DOM, so no page's types
// reach their code; and the code that runs in a page, typed with it.

import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
const TSC = createRequire(import.meta.url).resolve("typescript/bin/tsc");
// Each file a program compiles, the libraries it is typed with among them, by its path in this package.
const files = (config: string) => execFileSync(process.execPath, [TSC, "-p", config, "--listFilesOnly"], { cwd: DESKTOP, encoding: "utf8" })
  .split("\n").filter(Boolean).map((file) => relative(DESKTOP, file));

describe("the build's programs", { timeout: 60_000 }, () => {
  it("types the app's Node processes without the DOM, the main process among them", () => {
    const node = files("tsconfig.build.json");
    expect(node.filter((file) => file.includes("lib.dom"))).toEqual([]);
    expect(node).toEqual(expect.arrayContaining(["src/shell/main.ts", "src/vm/main.ts", "src/hosts/host.ts", "src/files/helper.ts"]));
    expect(node.filter((file) => /^src\/(shell\/pages\/|shell\/.*\.cts$|browser\/operations)/.test(file))).toEqual([]);
  });

  it("types what runs in a page with the DOM: the pages, their preloads, and the browser host's page scripts", () => {
    const pages = files("tsconfig.pages.json");
    // By its name alone: a linked node_modules puts the library elsewhere.
    expect(pages.some((file) => file.endsWith("typescript/lib/lib.dom.d.ts"))).toBe(true);
    expect(pages).toEqual(expect.arrayContaining([
      "src/shell/pages/shell.ts", "src/shell/pages-preload.cts", "src/shell/pane-preload.cts", "src/shell/preload.cts", "src/browser/operations.ts",
    ]));
  });
});

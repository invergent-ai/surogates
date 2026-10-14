// The app looks for the file helper's tools where its file hosts look for them, on every OS: both by one rule
// (hostToolsPath), the hosts by their own node and the app by the node it starts them on, so that what this OS puts
// first, as the app's own tools beside that node, is looked in by both. The app's own PATH alone is not where the
// hosts look: an app started from the Start menu, the Dock or a desktop's launcher has no tools folder of its own on it.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { expect, it } from "vitest";

const source = (file: string) => readFileSync(fileURLToPath(new URL(`../src/${file}`, import.meta.url)), "utf8").replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");

it("is one rule for the app's look and its hosts', each by the node the hosts run on", () => {
  const app = source("shell/main.ts");
  const host = source("hosts/host.ts");
  expect(app).toMatch(/fileToolsMissing\(BWRAP, hostToolsPath\(on, held, NODE\)\)/);
  expect(app).toMatch(/import \{ NODE \} from "\.\.\/hosts\/tool-hosts\.js";/);
  expect(host).toMatch(/hostToolsPath\(process\.env\.PATH, \[[^\]]*\], process\.execPath\)/);
  // Neither looks by a rule of its own.
  for (const [name, code] of [["main.ts", app], ["host.ts", host]] as const) {
    expect(code, name).not.toMatch(/\bpathOutside\(|srtOs\.toolsPath\(/);
  }
});

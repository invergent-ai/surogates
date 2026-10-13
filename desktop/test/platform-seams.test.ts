// Each part of the app that differs by OS picks its side by one lookup, platform.ts's forThisOs, as a row per OS:
// an OS the app is built for next is then one more row of each part, and never one more arm of a chain of tests.

import { readdirSync, readFileSync } from "node:fs";
import { join, posix } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, it } from "vitest";

const SRC = fileURLToPath(new URL("../src", import.meta.url));

const sources = (dir: string): string[] =>
  readdirSync(join(SRC, dir), { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? sources(posix.join(dir, entry.name)) : /\.[cm]?ts$/.test(entry.name) ? [posix.join(dir, entry.name)] : []);

// The code without its comments, which may say anything of an OS.
const code = (text: string) => text.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");

// Where the OS is told apart by its name in place: compared with one, or used to index a record.
const TOLD_APART = /process\.platform\s*[!=]==?|[!=]==?\s*process\.platform|\[\s*process\.platform\s*\]/;

// The modules that read the OS themselves, each for its reason.
const OWN = new Map([
  // On the guest's agent disk, which holds no platform.js (agent-disk-imports.test.ts).
  ["files/fd-paths.ts", "the agent's disk"],
  ["files/flags.ts", "the agent's disk"],
  ["files/helper.ts", "the agent's disk"],
  ["files/identity.ts", "the agent's disk"],
  // The pages' preload runs sandboxed, where it can import none of the app's modules.
  ["shell/pages-preload.cts", "a sandboxed preload"],
]);

it("tells the OS apart in no module but by forThisOs, but where a module cannot import it", () => {
  const telling = sources(".").filter((file) => TOLD_APART.test(code(readFileSync(join(SRC, file), "utf8"))));
  expect(telling.sort()).toEqual([...OWN.keys()].sort());
});

it("finds a chain of tests on the OS, and a record indexed by it, wherever they are written", () => {
  for (const told of [
    'process.platform === "linux" ? a : b', 'process.platform !== "darwin"', '"win32" === process.platform', "TAKE[process.platform] ?? x",
    "({ linux: 1 })[ process.platform ]", 'process.platform=="darwin"',
  ]) expect(TOLD_APART.test(told), told).toBe(true);
  for (const asked of ["backendFor(process.platform)", "forThisOs(rows, process.platform)", "// process.platform === \"linux\""]) expect(TOLD_APART.test(code(asked)), asked).toBe(false);
});

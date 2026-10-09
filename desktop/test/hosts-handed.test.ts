// A file host closes at its first line what it was handed beyond what it is meant to have. Run on
// the app's own node against dist/hosts/handed.js, from a parent that holds files open without
// close-on-exec at numbers Node does not mark by itself: above 15, behind one that is not open.

import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const NODE = fileURLToPath(new URL("../bin/node", import.meta.url));
const BUILT = fileURLToPath(new URL("../dist/hosts/handed.js", import.meta.url));
// What a child of the Node lists of its own descriptors, before and after *between* ran.
const CODE = (between: string) => `
import { spawnSync } from "node:child_process";
import { closeHanded } from ${JSON.stringify(BUILT)};
const lists = () => spawnSync("/bin/bash", ["-c", 'for fd in $(seq 0 60); do [ ! -e /proc/$$/fd/$fd ] || echo -n "$fd "; done'], { encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] }).stdout.trim();
const before = lists();
const closed = ${between};
console.log(JSON.stringify({ before, closed, after: lists() }));`;
const run = (between: string) => JSON.parse(spawnSync("/bin/bash", ["-c", 'exec 5</dev/null 30</dev/null 31</dev/null 47</dev/null; exec "$@"', "bash", NODE, "--input-type=module", "-e", CODE(between)], { encoding: "utf8" }).stdout) as unknown;

describe("a file host's first line", () => {
  it("closes what it was handed that a child of its own would inherit: the sandboxed helper then holds none of it", () => {
    // Without it: Node marks 5 by itself, and hands 30, 31 and 47 on.
    expect(run("0")).toEqual({ before: "0 1 2 30 31 47", closed: 0, after: "0 1 2 30 31 47" });
    expect(run("closeHanded()")).toEqual({ before: "0 1 2 30 31 47", closed: 3, after: "0 1 2" });
  });
});

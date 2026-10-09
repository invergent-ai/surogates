// A child the app starts holds the descriptors it is meant to have and nothing else of the app's.
// The parent here is a bash that holds two files open without close-on-exec, as Chromium holds its
// own in the app's main process: a Node of its own marks all it inherits at its start, so no test
// can hold one open in Node itself. Through the real app, the E2E files list each child's.

import { spawnSync } from "node:child_process";

import { describe, expect, it } from "vitest";

import { cleanly } from "../src/clean-child.js";

// *file* and *args*, run by a parent that holds descriptors 3, 7 and 8 open.
const held = (file: string, args: string[]) => spawnSync("/usr/bin/env", ["-i", "WORD=kept", "/bin/bash", "-c", 'exec 3</dev/null 7</dev/null 8</dev/null; exec "$@"', "bash", file, ...args], { encoding: "utf8" });
// A child that lists its own descriptors, by number.
const LISTS: [string, string[]] = ["/bin/bash", ["-c", 'for fd in $(seq 0 40); do [ ! -e /proc/$$/fd/$fd ] || echo -n "$fd "; done']];

describe("a child the app starts", () => {
  it("holds its three standard descriptors and nothing else, where one started as it is holds what its parent had open", () => {
    expect(held(...LISTS).stdout).toBe("0 1 2 3 7 8 ");
    expect(held(...cleanly(...LISTS)).stdout).toBe("0 1 2 ");
  });

  it("keeps as many descriptors as it is meant to have, a fourth for a channel among them", () => {
    expect(held(...cleanly(...LISTS, 3)).stdout).toBe("0 1 2 3 ");
    expect(held(...cleanly(...LISTS, 7)).stdout).toBe("0 1 2 3 7 ");
  });

  it("is the program itself: its arguments as they are, its exit and its process, and its environment to the name", () => {
    const itself = held("/bin/bash", ["-c", 'echo $$; exec "$@"', "bash", ...[cleanly("/bin/bash", ["-c", 'echo "$$ $0 $1 $2 $WORD"; exit 7', "a b", "c'd", "$(id)"])].flat(2)]);
    const [pid, said] = itself.stdout.trim().split("\n");
    expect([itself.status, said]).toEqual([7, `${pid} a b c'd $(id) kept`]);
    // What the parent named, and nothing a shell on the way would add (PWD, SHLVL).
    expect(spawnSync(...cleanly("/usr/bin/env", []), { encoding: "utf8", env: { WORD: "kept" } }).stdout).toBe("WORD=kept\n");
  });

  it("says so as a shell does, and ends 127, where the program is not there; and runs one by its whole path alone", () => {
    const gone = held(...cleanly("/nowhere/program", []));
    expect([gone.status, gone.stderr]).toEqual([127, "/nowhere/program: No such file or directory\n"]);
    expect(() => cleanly("pkexec", [])).toThrow("pkexec is not named by its whole path");
  });
});

// The VM's disks kept out of Time Machine on macOS: its folder made and excluded once, by a sticky
// exclusion that needs no administrator, and not asked again at the next start. Linux's backups are
// the user's own, and the app leaves its data as it is.

import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { keepOutOfTimeMachine, MARK } from "../src/shell/backups-darwin.js";
import { spawnsCleanly } from "./clean-spawns.js";
import { importedOn } from "./platforms.js";

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "backups-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("the VM's disks on macOS", () => {
  it("are kept out of Time Machine once: the folder made for its user alone, excluded, and marked", async () => {
    const vm = join(dir, "vm");
    const asked: string[][] = [];
    const run = async (args: string[]) => void asked.push(args);
    await keepOutOfTimeMachine(vm, run);
    expect(asked).toEqual([["addexclusion", vm]]);
    expect(statSync(vm).mode & 0o777).toBe(0o700);
    expect(existsSync(join(vm, MARK))).toBe(true);
    // The next start asks nothing.
    await keepOutOfTimeMachine(vm, run);
    expect(asked).toEqual([["addexclusion", vm]]);
  });

  it("are asked again at the next start where tmutil refused, and the refusal is said", async () => {
    const vm = join(dir, "vm");
    const asked: string[][] = [];
    await expect(keepOutOfTimeMachine(vm, async (args) => {
      asked.push(args);
      throw new Error("tmutil: no");
    })).rejects.toThrow("tmutil: no");
    expect(existsSync(join(vm, MARK))).toBe(false);
    await keepOutOfTimeMachine(vm, async (args) => void asked.push(args));
    expect(asked).toHaveLength(2);
  });

  it("are a Mac's to keep out, and Linux's backups are left to the user", async () => {
    const vm = join(dir, "vm");
    const linux = await importedOn("linux", () => import("../src/shell/backups.js"));
    await linux.keepOutOfBackups(vm);
    expect(existsSync(vm)).toBe(false);
  });
});

// tmutil started as the app starts it: waits for macOS's clean start of a program (clean-child.ts).
describe.runIf(process.platform === "darwin" && spawnsCleanly)("the VM's disks on a Mac", () => {
  it("are excluded, as tmutil isexcluded says", async () => {
    const vm = join(dir, "vm");
    const { keepOutOfBackups } = await import("../src/shell/backups.js");
    await keepOutOfBackups(vm);
    expect(execFileSync("/usr/bin/tmutil", ["isexcluded", vm], { encoding: "utf8" })).toMatch(/^\[Excluded\]/);
  });
});

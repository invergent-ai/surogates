// What an installed app takes as root's own word: its install record and the helper pkexec runs,
// each by its owner and its mode, and neither through a link. No test can own a file as root, so
// a test says who owns a path and what its mode is; every other call is real.

import type { Stats } from "node:fs";
import { mkdirSync, readdirSync, symlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

import { releaseKeys } from "../src/shell/updates.js";
import { installBase } from "../src/vm/image.js";
import { servedBase } from "./updates-base.js";

const told = vi.hoisted(() => ({
  // A path's owner and mode, as a look at it is to find them; *once*, at the next look alone.
  paths: new Map<string, { uid: number; mode?: number; once?: boolean }>(),
}));

vi.mock("node:fs", async (original) => {
  const fs = await original<typeof import("node:fs")>();
  const owned = <Look extends (...args: never[]) => unknown>(look: Look): Look => ((...args: Parameters<Look>) => {
    const found = look(...args) as Stats | undefined;
    const said = told.paths.get(String(args[0]));
    if (!found || !said) return found;
    if (said.once) told.paths.delete(String(args[0]));
    return Object.assign(found, { uid: said.uid, mode: said.mode === undefined ? found.mode : (found.mode & ~0o777) | said.mode });
  }) as Look;
  return { ...fs, statSync: owned(fs.statSync), lstatSync: owned(fs.lstatSync) };
});

const base = servedBase();
const NOT_ROOTS = "is not the install script's: only root may write it";
const own = process.getuid!();
// The path as root's own, at *mode*; or as *uid*'s.
const roots = (path: string, mode: number, uid = 0) => void told.paths.set(path, { uid, mode });

describe("what an installed app reads as root's own", () => {
  it("takes the release keys of a helper that root owns and no other may write, by its owner and by its mode, each alone", () => {
    expect(() => releaseKeys(base.helper, true)).toThrow(`${base.helper} ${NOT_ROOTS}`);
    roots(base.helper, 0o755);
    expect(releaseKeys(base.helper, true)).toHaveLength(1);
    // Root's, and its group may write it; root's, and anyone may; and another's that none but its owner may.
    for (const [mode, uid] of [[0o775, 0], [0o757, 0], [0o755, own], [0o700, own]] as const) {
      roots(base.helper, mode, uid);
      expect(() => releaseKeys(base.helper, true), `${mode.toString(8)} ${uid}`).toThrow(`${base.helper} ${NOT_ROOTS}`);
    }
    // A development build's helper is its test's own.
    expect(releaseKeys(base.helper)).toHaveLength(1);
  });

  it("takes the base of a record that root owns and no other may write, by its owner and by its mode, each alone", () => {
    expect(() => installBase(base.record, true)).toThrow(`${base.record} ${NOT_ROOTS}`);
    roots(base.record, 0o644);
    expect(installBase(base.record, true)).toBe(base.url);
    for (const [mode, uid] of [[0o664, 0], [0o646, 0], [0o644, own], [0o600, own]] as const) {
      roots(base.record, mode, uid);
      expect(() => installBase(base.record, true), `${mode.toString(8)} ${uid}`).toThrow(`${base.record} ${NOT_ROOTS}`);
    }
    expect(installBase(base.record)).toBe(base.url);
  });

  it("reads neither through a link, wherever it leads: a link to a file of root's is not root's own word", () => {
    // /etc/passwd is root's, and none but root may write it: by itself it passes as root's own.
    expect(() => releaseKeys("/etc/passwd", true)).toThrow("/etc/passwd trusts no release key");
    const helper = join(base.dir, "linked-helper");
    const record = join(base.dir, "linked-record");
    symlinkSync("/etc/passwd", helper);
    symlinkSync("/etc/passwd", record);
    expect(() => releaseKeys(helper, true)).toThrow(`${helper} is not the install script's: it is a link`);
    expect(() => installBase(record, true)).toThrow(`${record} is not the install script's: it is a link`);
    // Nor through a link that root itself owns, as one in a folder of root's would be.
    roots(helper, 0o777);
    roots(record, 0o777);
    expect(() => releaseKeys(helper, true)).toThrow(`${helper} is not the install script's: it is a link`);
    expect(() => installBase(record, true)).toThrow(`${record} is not the install script's: it is a link`);
    // A development build's are its test's own, and a link to one is read as it leads.
    const ownHelper = join(base.dir, "own-helper");
    const ownRecord = join(base.dir, "own-record");
    symlinkSync(base.helper, ownHelper);
    symlinkSync(base.record, ownRecord);
    expect(releaseKeys(ownHelper)).toHaveLength(1);
    expect(installBase(ownRecord)).toBe(base.url);
  });

  it("checks the helper as root's own where the record is, before anything is asked of the base: its keys are not taken when another may write it", async () => {
    base.publish("1.2.4");
    roots(base.record, 0o644);
    await expect(base.updates({ rootOwned: true }).check()).rejects.toThrow(`${base.helper} ${NOT_ROOTS}`);
    expect(base.heard).toEqual([]);
    // Root's, and its group may write it.
    roots(base.helper, 0o775);
    await expect(base.updates({ rootOwned: true }).check()).rejects.toThrow(`${base.helper} ${NOT_ROOTS}`);
    expect(base.heard).toEqual([]);
  });

  it("updates an installed app whose record and helper are root's own", async () => {
    base.publish("1.2.4");
    roots(base.record, 0o644);
    roots(base.helper, 0o755);
    const found = base.updates({ rootOwned: true });
    await found.check();
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
  });
});

describe("the update's cache, where a folder is not the user's own", () => {
  it("takes no folder there that another user owns: it goes, and one of the user's own is made", async () => {
    base.publish("1.2.4");
    const surogate = join(base.dir, "cache", "surogate");
    mkdirSync(join(surogate, "updates"), { recursive: true });
    writeFileSync(join(surogate, "theirs.txt"), "another user's\n");
    told.paths.set(surogate, { uid: own + 1, once: true });
    const found = base.updates();
    await found.check();
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
    expect(readdirSync(surogate)).toEqual(["updates"]);
  });
});

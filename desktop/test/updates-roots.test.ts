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
    return Object.assign(found, { uid: said.uid, mode: said.mode === undefined ? found.mode : (found.mode & ~0o7777) | said.mode });
  }) as Look;
  return { ...fs, statSync: owned(fs.statSync), lstatSync: owned(fs.lstatSync) };
});

const base = servedBase();
const NOT_ROOTS = "is not the install script's: only root may write it";
// What root's own reader of each asks, and no other mode (roots_own in release/install.sh).
const NOT_AS_LEFT = (mode: string) => `is not the install script's: an install leaves a file there, at mode ${mode}`;
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
    // Root's alone to write, and not at the mode an install leaves its helper: the helper refuses its
    // own list then, and every release with it, so the app offers none.
    for (const mode of [0o700, 0o555, 0o744, 0o644, 0o4755]) {
      told.paths.set(base.helper, { uid: 0, mode });
      expect(() => releaseKeys(base.helper, true), mode.toString(8)).toThrow(`${base.helper} ${NOT_AS_LEFT("755")}`);
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
    for (const mode of [0o600, 0o444, 0o640, 0o755, 0o2644]) {
      told.paths.set(base.record, { uid: 0, mode });
      expect(() => installBase(base.record, true), mode.toString(8)).toThrow(`${base.record} ${NOT_AS_LEFT("644")}`);
    }
    expect(installBase(base.record)).toBe(base.url);
  });

  it("takes for either a file alone: a folder of root's at the same mode is not one", () => {
    mkdirSync(join(base.dir, "folder"));
    roots(join(base.dir, "folder"), 0o755);
    expect(() => releaseKeys(join(base.dir, "folder"), true)).toThrow(`${join(base.dir, "folder")} ${NOT_AS_LEFT("755")}`);
    roots(join(base.dir, "folder"), 0o644);
    expect(() => installBase(join(base.dir, "folder"), true)).toThrow(`${join(base.dir, "folder")} ${NOT_AS_LEFT("644")}`);
  });

  it("reads neither through a link, wherever it leads: a link to a file of root's is not root's own word", () => {
    // /usr/bin/bash and /etc/passwd are root's, at the modes an install leaves its helper and its
    // record: by itself each passes as root's own.
    expect(() => releaseKeys("/usr/bin/bash", true)).toThrow("/usr/bin/bash trusts no release key");
    expect(() => installBase("/etc/passwd", true)).toThrow("/etc/passwd names no web address to download the sandbox from");
    const helper = join(base.dir, "linked-helper");
    const record = join(base.dir, "linked-record");
    symlinkSync("/usr/bin/bash", helper);
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

  it("says a newer version is installed for every user only from a mark that is root's own, as the helper takes one", async () => {
    base.publish("1.2.4");
    roots(base.record, 0o644);
    roots(base.helper, 0o755);
    const mark = join(base.dir, "release.json");
    writeFileSync(mark, `${JSON.stringify({ version: "1.2.9" })}\n`);
    // The test's own file, and root's at another mode than an install leaves: neither says what is installed.
    for (const said of [null, { uid: 0, mode: 0o664 }, { uid: 0, mode: 0o600 }, { uid: own, mode: 0o644 }]) {
      if (said) told.paths.set(mark, said);
      const found = base.updates({ rootOwned: true, installed: mark });
      await found.check();
      expect(found.state, JSON.stringify(said)).toMatchObject({ state: "available", version: "1.2.4" });
    }
    roots(mark, 0o644);
    base.heard = [];
    const found = base.updates({ rootOwned: true, installed: mark });
    await found.check();
    expect(found.state).toEqual({ state: "installed", version: "1.2.9" });
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

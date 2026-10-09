// What an installed app takes as root's own word: its install record and the helper pkexec runs,
// each by its owner and its mode, and neither through a link. No test can own a file as root, so
// a test says who owns a path and what its mode is; every other call is real.

import type { Stats } from "node:fs";
import { mkdirSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

import { releaseKeys, updateLine } from "../src/shell/updates.js";
import { installBase } from "../src/vm/image.js";
import { helperWith, keys, servedBase } from "./updates-base.js";

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
const NO_FILE = "is not the install script's: it is no file";
const NOT_RUN = "is not the install script's: it cannot be run";
const own = process.getuid!();
// The path as root's own, at *mode*; or as *uid*'s.
const roots = (path: string, mode: number, uid = 0) => void told.paths.set(path, { uid, mode });

describe("what an installed app reads as root's own", () => {
  it("takes the release keys of a helper that root owns, that no other may write and that can be run, at whatever mode", () => {
    expect(() => releaseKeys(base.helper, true)).toThrow(`${base.helper} ${NOT_ROOTS}`);
    // Root's alone to write, and a program: as an install leaves it, read-only, root's alone to run, with a set-id bit;
    // and one that only its group may run, or only others: root, whom pkexec runs it as, runs what anyone may.
    for (const mode of [0o755, 0o555, 0o700, 0o500, 0o744, 0o711, 0o4755, 0o610, 0o601]) {
      roots(base.helper, mode);
      expect(releaseKeys(base.helper, true), mode.toString(8)).toHaveLength(1);
    }
    // Root's, and its group may write it; root's, and anyone may; and another's that none but its owner may.
    for (const [mode, uid] of [[0o775, 0], [0o757, 0], [0o777, 0], [0o755, own], [0o700, own]] as const) {
      roots(base.helper, mode, uid);
      expect(() => releaseKeys(base.helper, true), `${mode.toString(8)} ${uid}`).toThrow(`${base.helper} ${NOT_ROOTS}`);
    }
    // Root's alone to write, and no program: pkexec could not run it.
    for (const mode of [0o644, 0o444, 0o600, 0o400]) {
      roots(base.helper, mode);
      expect(() => releaseKeys(base.helper, true), mode.toString(8)).toThrow(`${base.helper} ${NOT_RUN}`);
    }
    // A development build's helper is its test's own.
    expect(releaseKeys(base.helper)).toHaveLength(1);
  });

  it("takes the base of a record that root owns and no other may write, at whatever mode", () => {
    expect(() => installBase(base.record, true)).toThrow(`${base.record} ${NOT_ROOTS}`);
    // As an install leaves it, and as an administrator who keeps it read-only does.
    for (const mode of [0o644, 0o444, 0o600, 0o640, 0o400, 0o755]) {
      roots(base.record, mode);
      expect(installBase(base.record, true), mode.toString(8)).toBe(base.url);
    }
    for (const [mode, uid] of [[0o664, 0], [0o646, 0], [0o666, 0], [0o644, own], [0o600, own]] as const) {
      roots(base.record, mode, uid);
      expect(() => installBase(base.record, true), `${mode.toString(8)} ${uid}`).toThrow(`${base.record} ${NOT_ROOTS}`);
    }
    expect(installBase(base.record)).toBe(base.url);
  });

  it("takes for either a file alone: a folder of root's is not one", () => {
    mkdirSync(join(base.dir, "folder"));
    roots(join(base.dir, "folder"), 0o755);
    expect(() => releaseKeys(join(base.dir, "folder"), true)).toThrow(`${join(base.dir, "folder")} ${NO_FILE}`);
    expect(() => installBase(join(base.dir, "folder"), true)).toThrow(`${join(base.dir, "folder")} ${NO_FILE}`);
  });

  it("says to run the install script again where the helper is not one it can take, and why in what its check rejects with: it is not silent where the helper itself would explain", async () => {
    base.publish("1.2.4");
    roots(base.record, 0o644);
    const LINE = { text: "Surogate cannot update itself. Run the install script again.", button: null };
    // Each helper pkexec could not run, or whose keys are not root's own word, or that lists none.
    const helpers: Array<[string, () => void, string]> = [
      ["one no one may run", () => roots(base.helper, 0o644), `${base.helper} ${NOT_RUN}`],
      ["one its group may write", () => roots(base.helper, 0o775), `${base.helper} ${NOT_ROOTS}`],
      ["one that lists no release key", () => (writeFileSync(base.helper, helperWith([])), roots(base.helper, 0o755)), `${base.helper} trusts no release key`],
      ["none", () => (rmSync(base.helper), told.paths.delete(base.helper)), "ENOENT: no such file or directory"],
    ];
    for (const [name, make, why] of helpers) {
      writeFileSync(base.helper, helperWith([keys.publicKey]));
      make();
      const states: string[] = [];
      const found = base.updates({ rootOwned: true }, () => states.push(found.state.state));
      base.heard = [];
      await expect(found.check(), name).rejects.toThrow(why);
      expect([name, found.state, updateLine(found.state), states, base.heard]).toEqual([name, { state: "broken" }, LINE, ["broken"], []]);
      // Nothing is installed from it, and once the helper is as an install leaves it the line goes:
      // at the next check, also one that the base then leaves without an answer.
      await found.install();
      expect(found.state).toEqual({ state: "broken" });
      writeFileSync(base.helper, helperWith([keys.publicKey]));
      roots(base.helper, 0o755);
      const offered = base.served.get("/desktop/latest.json")!;
      base.served.delete("/desktop/latest.json");
      await expect(found.check(), name).rejects.toThrow("latest.json");
      expect([name, found.state, updateLine(found.state)]).toEqual([name, { state: "none" }, null]);
      base.served.set("/desktop/latest.json", offered);
      await found.check();
      expect([name, found.state.state, updateLine(found.state)?.text]).toEqual([name, "available", "Update available: Surogate 1.2.4"]);
    }
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
    // The test's own file, and root's that another may write: neither says what is installed.
    for (const said of [null, { uid: 0, mode: 0o664 }, { uid: 0, mode: 0o646 }, { uid: own, mode: 0o644 }]) {
      if (said) told.paths.set(mark, said);
      const found = base.updates({ rootOwned: true, installed: mark });
      await found.check();
      expect(found.state, JSON.stringify(said)).toMatchObject({ state: "available", version: "1.2.4" });
    }
    // Root's alone to write, at whatever mode: as an install leaves it, and read-only.
    for (const mode of [0o644, 0o444, 0o600]) {
      roots(mark, mode);
      base.heard = [];
      const found = base.updates({ rootOwned: true, installed: mark });
      await found.check();
      expect(found.state, mode.toString(8)).toEqual({ state: "installed", version: "1.2.9" });
      expect(base.heard).toEqual([]);
    }
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

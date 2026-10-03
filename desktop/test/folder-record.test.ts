import { spawn } from "node:child_process";
import { once } from "node:events";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  FolderBusy, lockFolder, presentIn, readRecord, removePlaceholders, writeRecord,
} from "../src/hosts/folder-record.js";

// The built module, for a holder in another process (npm test builds first).
const MODULE = new URL("../dist/hosts/folder-record.js", import.meta.url).href;

let dir: string;
let folder: string;

beforeEach(() => {
  dir = realpathSync(mkdtempSync(join(tmpdir(), "record-")));
  folder = join(dir, "folder");
  mkdirSync(folder);
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("the folder lock", () => {
  it("lets one holder have a folder at a time, and refuses the next once its wait is over", async () => {
    const held = await lockFolder(1, 2);
    const started = Date.now();
    await expect(lockFolder(1, 2, 300)).rejects.toBeInstanceOf(FolderBusy);
    expect(Date.now() - started).toBeGreaterThanOrEqual(300);
    // Another folder is another lock.
    (await lockFolder(1, 3)).close();
    held.close();
    (await lockFolder(1, 2, 0)).close();
  });

  it("waits for a holder that lets go while it waits", async () => {
    const held = await lockFolder(4, 5);
    setTimeout(() => held.close(), 200);
    (await lockFolder(4, 5, 2_000)).close();
  });

  it("is free as soon as its holder is killed", async () => {
    const holder = spawn(process.execPath, [
      "--input-type=module", "-e",
      `const { lockFolder } = await import(${JSON.stringify(MODULE)}); await lockFolder(7, 8); console.log("held"); setInterval(() => {}, 1000);`,
    ], { stdio: ["ignore", "pipe", "inherit"] });
    await once(holder.stdout, "data");
    await expect(lockFolder(7, 8, 0)).rejects.toBeInstanceOf(FolderBusy);
    holder.kill("SIGKILL");
    await once(holder, "exit");
    (await lockFolder(7, 8, 0)).close();
  });
});

describe("the folder record", () => {
  it("is read back as written, and is nothing when missing or not a record", () => {
    const path = join(dir, "folders", "1-2.json");
    expect(readRecord(path)).toBeNull();
    writeRecord(path, { state: "running", present: [".bashrc"], hooks: { "/f/.git/hooks/x": "1:2:3:4" } });
    expect(readRecord(path)).toEqual({ state: "running", present: [".bashrc"], hooks: { "/f/.git/hooks/x": "1:2:3:4" } });
    writeRecord(path, { state: "stopped", present: [], hooks: null });
    expect(readRecord(path)).toEqual({ state: "stopped", present: [], hooks: null });
    expect(readdirSync(join(dir, "folders"))).toEqual(["1-2.json"]);
    writeFileSync(path, "{");
    expect(readRecord(path)).toBeNull();
    writeFileSync(path, JSON.stringify({ state: "odd", present: [], hooks: null }));
    expect(readRecord(path)).toBeNull();
    writeFileSync(path, JSON.stringify({ state: "running", present: [], hooks: [] }));
    expect(readRecord(path)).toBeNull();
  });
});

describe("srt's placeholders", () => {
  it("are listed by the names that are there", () => {
    writeFileSync(join(folder, ".bashrc"), "");
    mkdirSync(join(folder, ".claude", "commands"), { recursive: true });
    expect(presentIn(folder)).toEqual([".bashrc", ".claude/commands", ".claude"]);
  });

  it("are removed after a crash only where provably srt's: empty and read-only, or an empty folder, over a name that was absent", () => {
    // What srt leaves while a command runs: empty 0444 files, and the .claude folder.
    for (const name of [".bashrc", ".vscode", ".mcp.json"]) writeFileSync(join(folder, name), "", { mode: 0o444 });
    mkdirSync(join(folder, ".claude"));
    writeFileSync(join(folder, ".claude", "commands"), "", { mode: 0o444 });
    mkdirSync(join(folder, ".git"));
    writeFileSync(join(folder, ".git", "hooks"), "", { mode: 0o444 });
    // The user's: there before, or not empty, or writable, or a folder with something in it.
    writeFileSync(join(folder, ".profile"), "", { mode: 0o444 });
    writeFileSync(join(folder, ".zshrc"), "export A=1\n", { mode: 0o444 });
    writeFileSync(join(folder, ".gitconfig"), "", { mode: 0o644 });
    mkdirSync(join(folder, ".idea"));
    writeFileSync(join(folder, ".idea", "x.xml"), "<x/>");
    removePlaceholders(folder, [".profile"]);
    expect(readdirSync(folder).sort()).toEqual([".git", ".gitconfig", ".idea", ".profile", ".zshrc"]);
    expect(readdirSync(join(folder, ".git"))).toEqual([]);
  });

  it("never follows a .git that a command made a link, out of the folder", () => {
    const outside = join(dir, "elsewhere");
    mkdirSync(join(outside, "hooks"), { recursive: true });
    writeFileSync(join(outside, "config"), "", { mode: 0o444 });
    symlinkSync(outside, join(folder, ".git"));
    removePlaceholders(folder, []);
    expect(existsSync(join(outside, "hooks"))).toBe(true);
    expect(existsSync(join(outside, "config"))).toBe(true);
  });
});

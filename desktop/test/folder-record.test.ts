import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdirSync, mkdtempSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FolderBusy, lockFolder, readRecord, writeRecord } from "../src/hosts/folder-record.js";

// The built module, for a holder in another process (npm test builds first).
const MODULE = new URL("../dist/hosts/folder-record.js", import.meta.url).href;

// Lock names are global per user on the machine: a device of our own process id
// keeps them apart from a suite running at the same time.
const D = process.pid;

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
    const held = await lockFolder(D, 2);
    const started = Date.now();
    await expect(lockFolder(D, 2, 300)).rejects.toBeInstanceOf(FolderBusy);
    expect(Date.now() - started).toBeGreaterThanOrEqual(300);
    // Another folder is another lock.
    (await lockFolder(D, 3)).close();
    held.close();
    (await lockFolder(D, 2, 0)).close();
  });

  it("waits for a holder that lets go while it waits", async () => {
    const held = await lockFolder(D, 5);
    setTimeout(() => held.close(), 200);
    (await lockFolder(D, 5, 2_000)).close();
  });

  it("is free as soon as its holder is killed", async () => {
    const holder = spawn(process.execPath, [
      "--input-type=module", "-e",
      `const { lockFolder } = await import(${JSON.stringify(MODULE)}); await lockFolder(${D}, 8); console.log("held"); setInterval(() => {}, 1000);`,
    ], { stdio: ["ignore", "pipe", "inherit"] });
    await once(holder.stdout, "data");
    await expect(lockFolder(D, 8, 0)).rejects.toBeInstanceOf(FolderBusy);
    holder.kill("SIGKILL");
    await once(holder, "exit");
    (await lockFolder(D, 8, 0)).close();
  });

  it("drops a connection to its name at once, and stays held", async () => {
    const held = await lockFolder(D, 9);
    const client = connect(`\0surogate-folder-${process.getuid?.() ?? 0}-${D}-9`);
    await once(client, "close");
    await expect(lockFolder(D, 9, 0)).rejects.toBeInstanceOf(FolderBusy);
    held.close();
  });
});

describe("the folder record", () => {
  it("is read back as written, and is nothing when missing or not a record", () => {
    const path = join(dir, "folders", "1-2.json");
    expect(readRecord(path)).toBeNull();
    writeRecord(path, { state: "running", hooks: { "/f/.git/hooks/x": "1:2:3:4" }, processes: [] });
    expect(readRecord(path)).toEqual({ state: "running", hooks: { "/f/.git/hooks/x": "1:2:3:4" }, processes: [] });
    writeRecord(path, { state: "stopped", hooks: null, processes: [] });
    expect(readRecord(path)).toEqual({ state: "stopped", hooks: null, processes: [] });
    expect(readdirSync(join(dir, "folders"))).toEqual(["1-2.json"]);
    writeFileSync(path, "{");
    expect(readRecord(path)).toBeNull();
    writeFileSync(path, JSON.stringify({ state: "odd", hooks: null }));
    expect(readRecord(path)).toBeNull();
    writeFileSync(path, JSON.stringify({ state: "running", hooks: [] }));
    expect(readRecord(path)).toBeNull();
    // An earlier build's, with the placeholders srt left: read as a record of now.
    writeFileSync(path, JSON.stringify({ state: "running", present: [".bashrc"], hooks: null, processes: [] }));
    expect(readRecord(path)).toEqual({ state: "running", hooks: null, processes: [] });
  });

  it("keeps the handles of a host's background processes, and has none in a record without them", () => {
    const path = join(dir, "folders", "1-3.json");
    const handle = { id: "proc_0123456789ab", command: "sleep 1", cwd: "/f", task_id: "t", started_at: 1_700_000_000.5 };
    writeRecord(path, { state: "stopped", hooks: null, processes: [handle] });
    expect(readRecord(path)?.processes).toEqual([handle]);
    writeFileSync(path, JSON.stringify({ state: "stopped", hooks: null, processes: [handle, { id: 1 }] }));
    expect(readRecord(path)?.processes).toEqual([handle]);
    writeFileSync(path, JSON.stringify({ state: "stopped", hooks: null }));
    expect(readRecord(path)).toEqual({ state: "stopped", hooks: null, processes: [] });
  });
});

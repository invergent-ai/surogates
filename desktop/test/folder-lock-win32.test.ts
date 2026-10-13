// The folder lock on Windows (src/hosts/folder-lock-win32.ts): a named pipe's first instance, which one process holds at
// a time, named by the user's SID, and free when its holder dies. It needs dist/ (tsc) and dist/native/ntat.node, and
// runs on Windows alone.

import { spawn } from "node:child_process";
import { once } from "node:events";
import { connect } from "node:net";
import { createInterface } from "node:readline";

import { describe, expect, it } from "vitest";

const WINDOWS = process.platform === "win32";
// Lock names are global per user on the machine: a device of our own process id keeps them apart from another run.
const D = process.pid;

describe.runIf(WINDOWS)("the folder lock on Windows", { timeout: 30_000 }, () => {
  it("is taken once, refused while it is held, and free once let go", async () => {
    const { takeLock } = await import("../src/hosts/folder-lock.js");
    const held = await takeLock(0, D, 21);
    expect(held).not.toBeNull();
    expect(await takeLock(0, D, 21)).toBeNull();
    // The uid Windows has none of names nothing: the user's SID names the lock.
    expect(await takeLock(1, D, 21)).toBeNull();
    const other = await takeLock(0, D, 22);
    expect(other).not.toBeNull();
    other?.close();
    held?.close();
    const again = await takeLock(0, D, 21);
    expect(again).not.toBeNull();
    again?.close();
  });

  it("is the user's SID and the folder's in a pipe's name, which drops whatever connects", async () => {
    const [{ takeLock }, { lockPipe }, { ntat }] = await Promise.all([
      import("../src/hosts/folder-lock.js"), import("../src/hosts/folder-lock-win32.js"), import("../src/files/ntat.js"),
    ]);
    expect(lockPipe(D, 23)).toBe(`\\\\.\\pipe\\surogate-folder-${ntat().userSid()}-${D}-23`);
    const held = await takeLock(0, D, 23);
    const client = connect(lockPipe(D, 23));
    await once(client, "close");
    expect(await takeLock(0, D, 23)).toBeNull();
    held?.close();
  });

  it("is free at once when the process that holds it is killed", async () => {
    const MODULE = new URL("../dist/hosts/folder-lock.js", import.meta.url).href;
    const holder = spawn(process.execPath, ["--input-type=module", "-e",
      `const { takeLock } = await import(${JSON.stringify(MODULE)}); const lock = await takeLock(0, ${D}, 25); console.log(lock ? "held" : "busy"); setInterval(() => {}, 1000);`,
    ], { stdio: ["ignore", "pipe", "inherit"] });
    const said = await new Promise<string>((resolve) => createInterface({ input: holder.stdout! }).once("line", resolve));
    expect(said).toBe("held");
    const { takeLock } = await import("../src/hosts/folder-lock.js");
    expect(await takeLock(0, D, 25)).toBeNull();
    holder.kill("SIGKILL");
    await once(holder, "exit");
    const free = await takeLock(0, D, 25);
    expect(free).not.toBeNull();
    free?.close();
  });
});

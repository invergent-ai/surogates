// The folder lock as each OS takes it, one try at a time; the host's wait for it is the folder record's (lockFolder).

import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtempSync, realpathSync, rmSync, statSync } from "node:fs";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";

import { afterAll, describe, expect, it, vi } from "vitest";

import { takeLock } from "../src/hosts/folder-lock.js";
import { importedOn } from "./platforms.js";

const UID = process.getuid?.() ?? 0;
// Lock names are global per user on the machine: a device of our own process id keeps them apart from a suite
// running at the same time.
const D = process.pid;
// The app's data folder, which macOS keeps its locks in; Linux's are the kernel's, and take none.
const DATA = realpathSync(mkdtempSync(join(tmpdir(), "folder-lock-")));
afterAll(() => rmSync(DATA, { recursive: true, force: true }));

// The code a test makes the next listen fail with, as the kernel would; the listens are real otherwise.
const net = vi.hoisted(() => ({ failing: null as string | null }));
vi.mock("node:net", async (original) => {
  const real = await original<typeof import("node:net")>();
  return {
    ...real,
    createServer: (...args: Parameters<typeof real.createServer>) => {
      const server = real.createServer(...args);
      const code = net.failing;
      net.failing = null;
      if (code) {
        server.listen = (() => {
          process.nextTick(() => server.emit("error", Object.assign(new Error(`listen ${code}`), { code, syscall: "listen" })));
          return server;
        }) as typeof server.listen;
      }
      return server;
    },
  };
});

describe("the folder lock, one try at a time", () => {
  it("is taken once, refused while it is held, and free once let go; another user's is another lock", async () => {
    const held = await takeLock(UID, D, 21, DATA);
    expect(held).not.toBeNull();
    expect(await takeLock(UID, D, 21, DATA)).toBeNull();
    const other = await takeLock(UID + 1, D, 21, DATA);
    expect(other).not.toBeNull();
    other?.close();
    held?.close();
    const again = await takeLock(UID, D, 21, DATA);
    expect(again).not.toBeNull();
    again?.close();
  });

  it("is free at once when the process that holds it is killed", async () => {
    const MODULE = new URL("../dist/hosts/folder-lock.js", import.meta.url).href;
    const holder = spawn(process.execPath, ["--input-type=module", "-e",
      `const { takeLock } = await import(${JSON.stringify(MODULE)}); const lock = await takeLock(${UID}, ${D}, 25, ${JSON.stringify(DATA)}); console.log(lock ? "held" : "busy"); setInterval(() => {}, 1000);`,
    ], { stdio: ["ignore", "pipe", "inherit"] });
    const said = await new Promise<string>((resolve) => createInterface({ input: holder.stdout! }).once("line", resolve));
    expect(said).toBe("held");
    expect(await takeLock(UID, D, 25, DATA)).toBeNull();
    holder.kill("SIGKILL");
    await once(holder, "exit");
    const free = await takeLock(UID, D, 25, DATA);
    expect(free).not.toBeNull();
    free?.close();
  });

  it.runIf(process.platform === "linux")("is, on Linux, the user's and the folder's name in the abstract socket namespace, which drops whatever connects", async () => {
    const held = await takeLock(UID, D, 22);
    const client = connect(`\0surogate-folder-${UID}-${D}-22`);
    await once(client, "close");
    expect(await takeLock(UID, D, 22)).toBeNull();
    held?.close();
  });

  it.runIf(process.platform === "linux")("is held by another only where the kernel says the name is in use: any other failure is the host's to see", async () => {
    for (const code of ["EACCES", "EMFILE"]) {
      net.failing = code;
      await expect(takeLock(UID, D, 24), code).rejects.toMatchObject({ code });
    }
    const free = await takeLock(UID, D, 24);
    expect(free).not.toBeNull();
    free?.close();
  });

  it("is refused, in words, on a system it is not built for yet", async () => {
    const { takeLock: elsewhere } = await importedOn("win32", () => import("../src/hosts/folder-lock.js"));
    await expect(elsewhere(UID, D, 23)).rejects.toThrow(new Error("The folder lock is not built for win32 yet"));
  });
});

describe.runIf(process.platform === "darwin")("the folder lock on macOS", () => {
  it("is a file of the app's data, the user's and the folder's, which only the user may open, and none without that folder", async () => {
    const lock = await takeLock(UID, D, 26, DATA);
    expect(lock).not.toBeNull();
    expect(statSync(join(DATA, "locks", `${UID}-${D}-26`)).mode & 0o777).toBe(0o600);
    expect(statSync(join(DATA, "locks")).mode & 0o777).toBe(0o700);
    lock?.close();
    await expect(takeLock(UID, D, 27)).rejects.toThrow("the folder lock on macOS is a file in the app's data folder, and none was given");
  });
});

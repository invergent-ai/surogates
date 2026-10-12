// The folder lock as each OS takes it, one try at a time; the host's wait for it is the folder record's (lockFolder).

import { once } from "node:events";
import { connect } from "node:net";

import { describe, expect, it } from "vitest";

import { takeLock } from "../src/hosts/folder-lock.js";
import { importedOn } from "./platforms.js";

const UID = process.getuid?.() ?? 0;
// Lock names are global per user on the machine: a device of our own process id keeps them apart from a suite
// running at the same time.
const D = process.pid;

describe("the folder lock, one try at a time", () => {
  it("is taken once, refused while it is held, and free once let go; another user's is another lock", async () => {
    const held = await takeLock(UID, D, 21);
    expect(held).not.toBeNull();
    expect(await takeLock(UID, D, 21)).toBeNull();
    const other = await takeLock(UID + 1, D, 21);
    expect(other).not.toBeNull();
    other?.close();
    held?.close();
    const again = await takeLock(UID, D, 21);
    expect(again).not.toBeNull();
    again?.close();
  });

  it("is, on Linux, the user's and the folder's name in the abstract socket namespace, which drops whatever connects", async () => {
    const held = await takeLock(UID, D, 22);
    const client = connect(`\0surogate-folder-${UID}-${D}-22`);
    await once(client, "close");
    expect(await takeLock(UID, D, 22)).toBeNull();
    held?.close();
  });

  it("is refused, in words, on a system it is not built for yet", async () => {
    const { takeLock: elsewhere } = await importedOn("darwin", () => import("../src/hosts/folder-lock.js"));
    await expect(elsewhere(UID, D, 23)).rejects.toThrow(new Error("The folder lock is not built for this computer's system yet"));
  });
});

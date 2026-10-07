import { createHash, randomBytes } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setFlagsFromString } from "node:v8";
import { runInNewContext } from "node:vm";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connectDevice } from "../src/device.js";
import { MAX_PAYLOAD_BYTES, MAX_WRITE_BYTES } from "../src/files/answers.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { DeviceLink } from "../src/link/client.js";
import { CHUNK_BYTES, type Operation, type Outcome } from "../src/link/protocol.js";
import { APP_CLOSED, DAMAGED, type Executor, MALFORMED_TRANSFER, type OperationRunner } from "../src/operations/runner.js";
import { TransferReceiver } from "../src/operations/receiver.js";
import { FakeLinkServer } from "./fake-server.js";

let dir: string;
let server: FakeLinkServer;
let url: string | null;
let links: DeviceLink[];
let runner: OperationRunner;
let journals: OperationJournal[];

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "writes-"));
  server = new FakeLinkServer();
  url = null;
  links = [];
  journals = [];
});

afterEach(async () => {
  Buffer.allocUnsafe = ALLOC_UNSAFE;
  for (const link of links) await link.stop();
  await server.stop();
  for (const journal of journals) journal.close();
  rmSync(dir, { recursive: true, force: true });
});

// Three chunks, the last one short.
const DATA = randomBytes(CHUNK_BYTES * 2 + 7);
const NAMED = { size: DATA.length, sha256: createHash("sha256").update(DATA).digest("hex") };

function writeOp(id: string, transfer: Record<string, unknown> = NAMED): Record<string, unknown> {
  return {
    type: "op", id, session_id: "r", calling_session_id: "r", invocation_id: "1:c",
    ordinal: 1, kind: "write", args: { key: "/f/big.bin", transfer }, digest: `digest-${id}`,
  };
}

const piece = (seq: number, data: Buffer = DATA) => data.subarray(seq * CHUNK_BYTES, (seq + 1) * CHUNK_BYTES);

function chunk(id: string, seq: number, data: Buffer = DATA): Record<string, unknown> {
  return { type: "chunk", id, seq, data: piece(seq, data).toString("base64") };
}

// What holds a write's data, seen from outside: each buffer of its size made meanwhile,
// weakly, as the receiver makes one for each write's data; and a full collection.
const ALLOC_UNSAFE = Buffer.allocUnsafe;
function watch(size: number): Array<WeakRef<Buffer>> {
  const made: Array<WeakRef<Buffer>> = [];
  Buffer.allocUnsafe = (bytes: number) => {
    const buffer = ALLOC_UNSAFE.call(Buffer, bytes);
    if (bytes === size) made.push(new WeakRef(buffer));
    return buffer;
  };
  return made;
}
setFlagsFromString("--expose-gc");
const gc = runInNewContext("gc") as () => void;

// Records what it is asked to admit and to run, and how many chunks the server had
// sent by then; it lets everything run, and answers each write with ok. It counts
// each time it is asked to end local work.
class Recording implements Executor {
  readonly admitted: Array<{ id: string; args: Record<string, unknown>; chunksSent: number }> = [];
  readonly ran: Operation[] = [];
  ended = 0;

  admit(operation: Operation): Promise<Outcome | null> {
    this.admitted.push({ id: operation.id, args: operation.args, chunksSent: sentChunks(operation.id) });
    return Promise.resolve(null);
  }

  run(operation: Operation): Promise<Outcome> {
    this.ran.push(operation);
    return Promise.resolve({ ok: null });
  }

  end(): Promise<void> {
    this.ended += 1;
    return Promise.resolve();
  }
}

let sent: Record<string, unknown>[] = [];
const sentChunks = (id: string) => sent.filter((f) => f.type === "chunk" && f.id === id).length;
const send = (frame: Record<string, unknown>) => {
  sent.push(frame);
  server.send(frame);
};

function open(): OperationJournal {
  const journal = new OperationJournal(join(dir, "journal.sqlite"));
  journals.push(journal);
  return journal;
}

async function start(executor: Executor, journal = open()): Promise<DeviceLink> {
  sent = [];
  url ??= await server.start();
  const device = connectDevice({ url, token: "surg_dev_test", journal, executor, onError: () => {}, delay: () => 20 });
  links.push(device.link);
  runner = device.runner;
  device.link.start();
  await server.until(() => device.link.status === "connected");
  return device.link;
}

const results = (id: string) => server.received.filter((f) => f.type === "op_result" && f.id === id);
const acks = (id: string) => server.received.filter((f) => f.type === "chunk_ack" && f.id === id).map((f) => f.seq);
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe("a write whose data comes in a transfer", () => {
  it("is asked about and run only once its data is whole and checked, with that data, as a write that carries it", async () => {
    const executor = new Recording();
    await start(executor);
    send(writeOp("a"));
    send(chunk("a", 0));
    send(chunk("a", 1));
    await server.until(() => acks("a").length === 2);
    await pause(50);
    expect(executor.admitted).toEqual([]);
    send(chunk("a", 2));
    await server.until(() => results("a").length === 1);
    // Every chunk acknowledged; asked about and run with its data in place of the transfer.
    expect(acks("a")).toEqual([0, 1, 2]);
    expect(executor.admitted).toEqual([{ id: "a", args: { key: "/f/big.bin", data: DATA.toString("base64") }, chunksSent: 3 }]);
    expect(executor.ran.map((op) => op.args)).toEqual([{ key: "/f/big.bin", data: DATA.toString("base64") }]);
    expect(results("a")[0]?.outcome).toEqual({ ok: null });
  });

  it("runs with the args it came with, a revision it expects too", async () => {
    const executor = new Recording();
    await start(executor);
    const args = { key: "/f/big.bin", transfer: NAMED, expected_revision: "1:2:3:4:5" };
    send({ ...writeOp("a"), args });
    for (const seq of [0, 1, 2]) send(chunk("a", seq));
    await server.until(() => results("a").length === 1);
    expect(executor.ran.map((op) => op.args)).toEqual([
      { key: "/f/big.bin", expected_revision: "1:2:3:4:5", data: DATA.toString("base64") },
    ]);
  });

  it("whose data does not match its SHA-256 is answered so, and never run", async () => {
    const executor = new Recording();
    await start(executor);
    send(writeOp("a", { ...NAMED, sha256: "0".repeat(64) }));
    for (const seq of [0, 1, 2]) send(chunk("a", seq));
    await server.until(() => results("a").length === 1);
    expect(results("a")[0]?.outcome).toEqual(DAMAGED);
    expect(executor.admitted).toEqual([]);
    expect(executor.ran).toEqual([]);
  });

  it.each([
    ["out of order", [1, 0, 2]],
    ["with a chunk missing", [0, 2]],
  ])("whose chunks come %s is answered as damaged, and never run", async (_name, order) => {
    const executor = new Recording();
    await start(executor);
    send(writeOp("a"));
    for (const seq of order) send(chunk("a", seq));
    await server.until(() => results("a").length === 1);
    expect(results("a")[0]?.outcome).toEqual(DAMAGED);
    expect(executor.ran).toEqual([]);
  });

  it("whose last chunk is too long is answered as damaged, and never run", async () => {
    const executor = new Recording();
    await start(executor);
    send(writeOp("a"));
    send(chunk("a", 0));
    send(chunk("a", 1));
    send({ ...chunk("a", 2), data: Buffer.alloc(8).toString("base64") });
    await server.until(() => results("a").length === 1);
    expect(results("a")[0]?.outcome).toEqual(DAMAGED);
    expect(executor.ran).toEqual([]);
  });

  it("whose data comes beside it as well is answered, and never run", async () => {
    const executor = new Recording();
    await start(executor);
    send({ ...writeOp("a"), args: { key: "/f/big.bin", transfer: NAMED, data: "aGk=" } });
    await server.until(() => results("a").length === 1);
    expect(results("a")[0]?.outcome).toEqual(MALFORMED_TRANSFER);
    expect(executor.admitted).toEqual([]);
    expect(executor.ran).toEqual([]);
  });

  it("holds its data only as the base64 it runs with, once it runs", async () => {
    const made = watch(DATA.length);
    let held: boolean | undefined;
    const executor = new Recording();
    executor.run = async (operation) => {
      executor.ran.push(operation);
      await pause(10);
      gc();
      held = made[0]?.deref() !== undefined;
      return { ok: null };
    };
    await start(executor);
    send(writeOp("a"));
    for (const seq of [0, 1, 2]) send(chunk("a", seq));
    await server.until(() => results("a").length === 1);
    expect(made).toHaveLength(1);
    expect(held).toBe(false);
  });

  it.each([
    ["no more than 1 MiB", { ...NAMED, size: MAX_PAYLOAD_BYTES }],
    ["over 50 MiB", { ...NAMED, size: MAX_WRITE_BYTES + 1 }],
    ["a fractional size", { ...NAMED, size: DATA.length + 0.5 }],
    ["an upper-case SHA-256", { ...NAMED, sha256: NAMED.sha256.toUpperCase() }],
    ["a short SHA-256", { ...NAMED, sha256: "0".repeat(63) }],
    ["another key", { ...NAMED, extra: 1 }],
  ])("whose transfer names %s is answered, and never run", async (_name, transfer) => {
    const executor = new Recording();
    await start(executor);
    send(writeOp("a", transfer));
    await server.until(() => results("a").length === 1);
    expect(results("a")[0]?.outcome).toEqual(MALFORMED_TRANSFER);
    expect(executor.admitted).toEqual([]);
    expect(executor.ran).toEqual([]);
  });

  it("stopped while its data comes never runs; the rest of its chunks are acknowledged and dropped", async () => {
    const executor = new Recording();
    await start(executor);
    send(writeOp("a"));
    send(chunk("a", 0));
    send({ type: "cancel", id: "a" });
    send(chunk("a", 1));
    send(chunk("a", 2));
    await server.until(() => acks("a").length === 3);
    await pause(50);
    expect(executor.admitted).toEqual([]);
    expect(executor.ran).toEqual([]);
    expect(results("a")).toEqual([]);
  });

  it("stopped while its data comes, with no more of it, is let go: never run, and nothing waits on it", async () => {
    const executor = new Recording();
    await start(executor);
    send(writeOp("a"));
    send(chunk("a", 0));
    await server.until(() => acks("a").length === 1);
    // The server sends no more of a cancelled write's data, so the cancel alone must end its wait.
    send({ type: "cancel", id: "a" });
    await server.until(() => journals[0]?.openIds().length === 0);
    let settled = false;
    void runner.suspend(APP_CLOSED).then(() => {
      settled = true;
    });
    await server.until(() => settled);
    expect(executor.admitted).toEqual([]);
    expect(executor.ran).toEqual([]);
    expect(results("a")).toEqual([]);
  });

  it("whose data stops part-way lets a revocation end the computer's access, and never runs", async () => {
    const executor = new Recording();
    await start(executor);
    send(writeOp("a"));
    send(chunk("a", 0));
    await server.until(() => acks("a").length === 1);
    server.close(4403);
    await server.until(() => executor.ended === 1);
    expect(executor.admitted).toEqual([]);
    expect(executor.ran).toEqual([]);
    // Dropped while it waited for its data: still received, so the server sends it again.
    expect(journals[0]?.openIds()).toEqual(["a"]);
  });

  it("starts its data over from chunk 0 on a new connection, and runs once", async () => {
    const executor = new Recording();
    await start(executor);
    send(writeOp("a"));
    send(chunk("a", 0));
    send(chunk("a", 1));
    await server.until(() => acks("a").length === 2);
    server.close(1011);
    await server.until(() => server.connections === 2 && links[0]?.status === "connected");
    send(writeOp("a"));
    for (const seq of [0, 1, 2]) send(chunk("a", seq));
    await server.until(() => results("a").length === 1);
    expect(executor.ran.map((op) => op.args.data)).toEqual([DATA.toString("base64")]);
  });

  it("is lost with what came of its data when the app stops, reported open, and runs once sent again", async () => {
    const first = new Recording();
    const link = await start(first);
    send(writeOp("a"));
    send(chunk("a", 0));
    await server.until(() => acks("a").length === 1);
    await link.stop();
    journals.pop()?.close();

    const second = new Recording();
    await start(second);
    expect(server.hellos.at(-1)?.open).toEqual(["a"]);
    send(writeOp("a"));
    for (const seq of [0, 1, 2]) send(chunk("a", seq));
    await server.until(() => results("a").length === 1);
    expect(first.ran).toEqual([]);
    expect(second.ran.map((op) => op.args.data)).toEqual([DATA.toString("base64")]);
  });

  it("whose data is already whole takes the chunks a new connection sends again, and runs once", async () => {
    // Held in admit, as a prompt the user has not answered yet holds it.
    let allow: () => void = () => {};
    const executor = new Recording();
    executor.admit = (operation) => {
      executor.admitted.push({ id: operation.id, args: operation.args, chunksSent: sentChunks(operation.id) });
      return new Promise((resolve) => {
        allow = () => resolve(null);
      });
    };
    await start(executor);
    send(writeOp("a"));
    for (const seq of [0, 1, 2]) send(chunk("a", seq));
    await server.until(() => executor.admitted.length === 1);
    server.close(1011);
    await server.until(() => server.connections === 2 && links[0]?.status === "connected");
    send(writeOp("a"));
    for (const seq of [0, 1, 2]) send(chunk("a", seq));
    await server.until(() => acks("a").length === 6);
    allow();
    await server.until(() => results("a").length === 1);
    expect(executor.admitted).toHaveLength(1);
    expect(executor.ran.map((op) => op.args.data)).toEqual([DATA.toString("base64")]);
  });
});

describe("a write that carries its data", () => {
  it("runs as it came, with nothing to wait for", async () => {
    const executor = new Recording();
    await start(executor);
    send({ ...writeOp("a"), args: { key: "/f/a.txt", data: "aGk=" } });
    await server.until(() => results("a").length === 1);
    expect(executor.ran.map((op) => op.args)).toEqual([{ key: "/f/a.txt", data: "aGk=" }]);
  });

  it("of up to 1 MiB runs; more is answered and never run, since larger data comes in a transfer", async () => {
    const executor = new Recording();
    await start(executor);
    const most = Buffer.alloc(MAX_PAYLOAD_BYTES).toString("base64");
    send({ ...writeOp("a"), args: { key: "/f/a.bin", data: most } });
    send({ ...writeOp("b"), args: { key: "/f/b.bin", data: Buffer.alloc(MAX_PAYLOAD_BYTES + 1).toString("base64") } });
    await server.until(() => results("a").length === 1 && results("b").length === 1);
    expect(results("a")[0]?.outcome).toEqual({ ok: null });
    expect(results("b")[0]?.outcome).toEqual(MALFORMED_TRANSFER);
    expect(executor.admitted.map((op) => op.id)).toEqual(["a"]);
    expect(executor.ran.map((op) => op.id)).toEqual(["a"]);
  });
});

describe("the data of writes, as it comes", () => {
  it("starts again in a new buffer on a new connection, letting go of what came on the last", async () => {
    const made = watch(DATA.length);
    const receiver = new TransferReceiver();
    const whole = receiver.whole("a", NAMED, new AbortController().signal);
    receiver.chunk("a", 0, piece(0));
    receiver.restart();
    for (const seq of [0, 1, 2]) receiver.chunk("a", seq, piece(seq));
    const data = await whole;
    expect(data?.equals(DATA)).toBe(true);
    expect(made).toHaveLength(2);
    expect(made[1]?.deref()).toBe(data);
    await pause(10);
    gc();
    expect(made[0]?.deref()).toBeUndefined();
  });

  it("drops a chunk for a write whose data is whole, after a new connection too, and leaves that data as it was", async () => {
    const made = watch(DATA.length);
    const receiver = new TransferReceiver();
    const whole = receiver.whole("a", NAMED, new AbortController().signal);
    for (const seq of [0, 1, 2]) receiver.chunk("a", seq, piece(seq));
    const data = await whole;
    receiver.restart();
    receiver.chunk("a", 0, Buffer.alloc(CHUNK_BYTES));
    expect(data?.equals(DATA)).toBe(true);
    expect(made).toHaveLength(1);
  });
});

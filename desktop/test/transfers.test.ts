import { createHash, randomBytes } from "node:crypto";
import { mkdtempSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { DatabaseSync } from "node:sqlite";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { connectDevice } from "../src/device.js";
import { MAX_PAYLOAD_BYTES, MAX_READ_BYTES } from "../src/files/answers.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { DeviceLink } from "../src/link/client.js";
import { CHUNK_BYTES, type Operation, type Outcome, TRANSFER_WINDOW } from "../src/link/protocol.js";
import { type Executor, TOO_LARGE } from "../src/operations/runner.js";
import { type Send, TransferSender } from "../src/operations/transfers.js";
import { FakeLinkServer } from "./fake-server.js";

let dir: string;
let server: FakeLinkServer;
let link: DeviceLink | null;
let journal: OperationJournal;
let errors: unknown[];

beforeEach(() => {
  errors = [];
  dir = mkdtempSync(join(tmpdir(), "transfers-"));
  server = new FakeLinkServer({ heartbeatS: 1 });
  link = null;
  journal = new OperationJournal(join(dir, "journal.sqlite"));
});

afterEach(async () => {
  await link?.stop();
  await server.stop();
  journal.close();
  rmSync(dir, { recursive: true, force: true });
});

// Seven chunks, the last one short.
const DATA = randomBytes(CHUNK_BYTES * 6 + 7);
const SHA256 = createHash("sha256").update(DATA).digest("hex");
const NAMED = { ok: { transfer: { size: DATA.length, sha256: SHA256 } } };

function readOp(id: string): Record<string, unknown> {
  return {
    type: "op", id, session_id: "r", calling_session_id: "r", invocation_id: "1:c",
    ordinal: 1, kind: "read", args: { key: "/f/big.bin", max_bytes: null }, digest: `digest-${id}`,
  };
}

// Answers every read with *data*, as the file helper does.
function reading(data: Buffer, ran: string[] = []): Executor {
  return {
    run(operation: Operation): Promise<Outcome> {
      ran.push(operation.id);
      return Promise.resolve({ ok: data.toString("base64") });
    },
  };
}

async function start(executor: Executor): Promise<void> {
  const url = await server.start();
  const device = connectDevice({ url, token: "surg_dev_test", journal, executor, onError: (error) => errors.push(error), delay: () => 20 });
  link = device.link;
  device.link.start();
  await server.until(() => device.link.status === "connected");
}

const headers = (id: string) => server.received.filter((f) => f.type === "op_result" && f.id === id);
const chunks = (id: string) => server.received.filter((f) => f.type === "chunk" && f.id === id);
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const joined = (frames: Record<string, unknown>[]) => Buffer.concat(frames.map((f) => Buffer.from(String(f.data), "base64")));

describe("a read too large for one frame", () => {
  it("goes as a header naming it, then its chunks, at most four ahead of the server's acknowledgements", async () => {
    await start(reading(DATA));
    server.send(readOp("a"));
    await server.until(() => chunks("a").length === TRANSFER_WINDOW);
    await pause(100);
    expect(chunks("a")).toHaveLength(TRANSFER_WINDOW);
    expect(headers("a").map((f) => f.outcome)).toEqual([NAMED]);
    // Each acknowledgement lets one more go.
    server.send({ type: "chunk_ack", id: "a", seq: 0 });
    await server.until(() => chunks("a").length === TRANSFER_WINDOW + 1);
    for (let seq = 1; seq < 6; seq++) server.send({ type: "chunk_ack", id: "a", seq });
    await server.until(() => chunks("a").length === 7);
    expect(chunks("a").map((f) => f.seq)).toEqual([0, 1, 2, 3, 4, 5, 6]);
    expect(joined(chunks("a")).equals(DATA)).toBe(true);
    server.send({ type: "op_ack", id: "a" });
    await server.until(() => journal.unsent().length === 0);
    // Acknowledged: its chunks are gone from the journal.
    expect(journal.chunk("a", 0)).toBeNull();
  });

  it("keeps the pings going while the server holds the transfer's acknowledgements back", async () => {
    await start(reading(DATA));
    server.send(readOp("a"));
    await server.until(() => chunks("a").length === TRANSFER_WINDOW);
    const pings = () => server.received.filter((f) => f.type === "ping").length;
    const before = pings();
    await server.until(() => pings() >= before + 2, 4_000);
    expect(link?.status).toBe("connected");
  });

  it("starts over from its header and chunk 0 on the next connection", async () => {
    await start(reading(DATA));
    server.send(readOp("a"));
    await server.until(() => chunks("a").length === TRANSFER_WINDOW);
    server.send({ type: "chunk_ack", id: "a", seq: 0 });
    await server.until(() => chunks("a").length === TRANSFER_WINDOW + 1);
    server.close(1011);
    await server.until(() => server.connections === 2 && link?.status === "connected");
    await server.until(() => headers("a").length === 2 && chunks("a").length === 2 * TRANSFER_WINDOW + 1);
    expect(chunks("a").slice(TRANSFER_WINDOW + 1).map((f) => f.seq)).toEqual([0, 1, 2, 3]);
  });

  it("stops when the server does not want it, and is never sent again", async () => {
    await start(reading(DATA));
    server.send(readOp("a"));
    await server.until(() => chunks("a").length === TRANSFER_WINDOW);
    server.send({ type: "unwanted", id: "a" });
    await server.until(() => journal.unsent().length === 0);
    server.send({ type: "chunk_ack", id: "a", seq: 0 });
    await pause(100);
    expect(chunks("a")).toHaveLength(TRANSFER_WINDOW);
    expect(journal.chunk("a", 0)).toBeNull();
    server.close(1011);
    await server.until(() => server.connections === 2 && link?.status === "connected");
    await pause(100);
    expect(headers("a")).toHaveLength(1);
  });

  it("goes after the one before it, one transfer at a time", async () => {
    await start(reading(DATA));
    server.send(readOp("a"));
    server.send(readOp("b"));
    await server.until(() => chunks("a").length === TRANSFER_WINDOW);
    await pause(100);
    expect(headers("b")).toEqual([]);
    for (let seq = 0; seq < 6; seq++) server.send({ type: "chunk_ack", id: "a", seq });
    await server.until(() => chunks("a").length === 7);
    server.send({ type: "op_ack", id: "a" });
    await server.until(() => chunks("b").length === TRANSFER_WINDOW);
    expect(headers("b").map((f) => f.outcome)).toEqual([NAMED]);
  });

  it("is not sent twice when the server repeats its operation while it goes", async () => {
    const ran: string[] = [];
    await start(reading(DATA, ran));
    server.send(readOp("a"));
    await server.until(() => chunks("a").length === TRANSFER_WINDOW);
    server.send(readOp("a"));
    await pause(100);
    expect(ran).toEqual(["a"]);
    expect(headers("a")).toHaveLength(1);
    expect(chunks("a")).toHaveLength(TRANSFER_WINDOW);
  });
});

describe("a read's result", () => {
  it("of at most 1 MiB goes inline, in one frame", async () => {
    const data = randomBytes(MAX_PAYLOAD_BYTES);
    await start(reading(data));
    server.send(readOp("a"));
    await server.until(() => headers("a").length === 1);
    expect(headers("a")[0]?.outcome).toEqual({ ok: data.toString("base64") });
    expect(chunks("a")).toEqual([]);
  });

  it("over 50 MiB, which the file helper never sends, is answered too_large rather than refused by the server", async () => {
    await start(reading(Buffer.alloc(MAX_READ_BYTES + 1)));
    server.send(readOp("a"));
    await server.until(() => headers("a").length === 1);
    expect(headers("a")[0]?.outcome).toEqual(TOO_LARGE);
    expect(chunks("a")).toEqual([]);
  });
});

describe("a read whose chunks the journal cannot keep", () => {
  it("is answered that the disk is full, gives the space back, and the link stays up", async () => {
    await start(reading(DATA));
    // A full disk 4 MiB on: SQLite spills part of the write to the -wal, fails it and rolls it back by itself.
    const db = (journal as unknown as { db: DatabaseSync }).db;
    const { page_count: pages } = db.prepare("PRAGMA page_count").get() as { page_count: number };
    db.exec(`PRAGMA max_page_count = ${pages + 1024}`);
    server.send(readOp("a"));
    await server.until(() => headers("a").length === 1);
    expect(headers("a")[0]?.outcome).toEqual({
      error: { type: "os", code: "ENOSPC", message: "Not enough free disk space on this computer to send this file" },
    });
    expect(chunks("a")).toEqual([]);
    // The rollback leaves the -wal as large as the write got: the user's disk would stay full.
    expect(statSync(join(dir, "journal.sqlite-wal")).size).toBe(0);
    expect(errors).toEqual([]);
    expect(link?.status).toBe("connected");
  });

  it("for another reason is answered that it could not be kept, and the link stays up", async () => {
    await start(reading(DATA));
    const finish = journal.finish.bind(journal);
    vi.spyOn(journal, "finish").mockImplementation((id, outcome, chunks = []) => {
      if (chunks.length > 0) throw new Error("disk I/O error");
      return finish(id, outcome, chunks);
    });
    server.send(readOp("a"));
    await server.until(() => headers("a").length === 1);
    expect(headers("a")[0]?.outcome).toEqual({
      error: { type: "os", code: "EIO", message: "This computer could not keep this file to send it" },
    });
    expect(errors).toEqual([]);
    expect(link?.status).toBe("connected");
  });
});

describe("the sender", () => {
  it("hands ws a chunk only once ws has written the one before, and never more than four unacknowledged", () => {
    const frames: Record<string, unknown>[] = [];
    // ws calls these back once it has written the frame to the socket; here they wait for the test.
    const unwritten: Array<() => void> = [];
    const send: Send = (frame, written) => {
      frames.push(frame);
      if (written) unwritten.push(written);
      return true;
    };
    const sender = new TransferSender(send, (_id, seq) => (seq < 7 ? Buffer.alloc(1, seq) : null));
    const sent = () => frames.filter((f) => f.type === "chunk").map((f) => f.seq);
    const write = () => unwritten.shift()?.();

    sender.add("a", "digest-a", NAMED);
    expect(sent()).toEqual([0]);
    // An acknowledgement, or another transfer to send, hands ws nothing more while it holds chunk 0.
    sender.acked("a", 0);
    sender.add("b", "digest-b", NAMED);
    expect(sent()).toEqual([0]);
    write();
    expect(sent()).toEqual([0, 1]);
    write();
    write();
    write();
    expect(sent()).toEqual([0, 1, 2, 3, 4]);
    // Chunk 4 is written too, but 1 to 4 are not acknowledged: the window is full.
    write();
    expect(sent()).toEqual([0, 1, 2, 3, 4]);
    sender.acked("a", 1);
    expect(sent()).toEqual([0, 1, 2, 3, 4, 5]);
  });

  it("hands ws the next transfer's first chunk only once ws has written the last one's", () => {
    const frames: Record<string, unknown>[] = [];
    const unwritten: Array<() => void> = [];
    const send: Send = (frame, written) => {
      frames.push(frame);
      if (written) unwritten.push(written);
      return true;
    };
    const sender = new TransferSender(send, (_id, seq) => (seq < 7 ? Buffer.alloc(1, seq) : null));
    const sent = () => frames.map((f) => (f.type === "chunk" ? `${String(f.id)}${String(f.seq)}` : `${String(f.id)} header`));

    sender.add("a", "digest-a", NAMED);
    sender.add("b", "digest-b", NAMED);
    // The server does not want a while ws still holds a0: b's header goes, b0 waits for a0.
    sender.done("a");
    expect(sent()).toEqual(["a header", "a0", "b header"]);
    unwritten.shift()?.();
    expect(sent()).toEqual(["a header", "a0", "b header", "b0"]);
  });
});

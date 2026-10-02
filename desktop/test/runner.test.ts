import { once } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WebSocketServer } from "ws";

import { connectDevice } from "../src/device.js";
import { INTERRUPTED, OperationJournal } from "../src/journal/journal.js";
import type { DeviceLink } from "../src/link/client.js";
import { MAX_FRAME_CHARS, type Operation, type Outcome } from "../src/link/protocol.js";
import { type Executor, TOO_LARGE } from "../src/operations/runner.js";
import { FakeLinkServer } from "./fake-server.js";

let dir: string;
let server: FakeLinkServer;
let link: DeviceLink | null;
let journals: OperationJournal[];

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), "runner-"));
  server = new FakeLinkServer();
  link = null;
  journals = [];
});

afterEach(async () => {
  await link?.stop();
  await server.stop();
  for (const journal of journals) journal.close();
  rmSync(dir, { recursive: true, force: true });
});

// Every journal a test opens is closed after it, so the file can go.
function open(path = join(dir, "journal.sqlite")): OperationJournal {
  const journal = new OperationJournal(path);
  journals.push(journal);
  return journal;
}

function opFrame(id: string, kind = "which"): Record<string, unknown> {
  return {
    type: "op", id, session_id: "r", calling_session_id: "r", invocation_id: "1:c",
    ordinal: 1, kind, args: { name: "sh" }, digest: `digest-${id}`,
  };
}

class RecordingExecutor implements Executor {
  readonly ran: string[] = [];
  readonly aborted: string[] = [];
  private readonly held = new Map<string, (outcome: Outcome) => void>();

  constructor(private readonly hold = false) {}

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    this.ran.push(operation.id);
    if (!this.hold) return Promise.resolve({ ok: `ran ${operation.id}` });
    return new Promise((resolve) => {
      this.held.set(operation.id, resolve);
      signal.addEventListener("abort", () => {
        this.aborted.push(operation.id);
        resolve({ error: { type: "cancelled", message: "stopped" } });
      });
    });
  }

  finish(id: string): void {
    this.held.get(id)?.({ ok: `ran ${id}` });
  }
}

async function start(executor: Executor, journal = open(), onError?: (error: unknown) => void) {
  const url = await server.start();
  const device = connectDevice({ url, token: "surg_dev_test", journal, executor, onError, delay: () => 20 });
  link = device.link;
  device.link.start();
  await server.until(() => device.link.status === "connected");
  return { ...device, journal };
}

const results = (id: string) => server.received.filter((f) => f.type === "op_result" && f.id === id);

// What the server's record() takes of an op_result (surogates/devices/link.py): a frame
// of at most MAX_FRAME_CHARS whose outcome is a dict with exactly one of ok and error,
// and an error that is a dict. Anything else closes 4400, and the row is resent forever.
function accepted(frame: Record<string, unknown> | undefined): boolean {
  const outcome = frame?.outcome;
  if (typeof outcome !== "object" || outcome === null || Array.isArray(outcome)) return false;
  const record = outcome as Record<string, unknown>;
  const error = record.error;
  return JSON.stringify(frame).length <= MAX_FRAME_CHARS
    && ["ok", "error"].filter((key) => key in record).length === 1
    && (!("error" in record) || (typeof error === "object" && error !== null && !Array.isArray(error)));
}

describe("running an operation", () => {
  it("runs it once and answers; a repeat gets the same answer without running again", async () => {
    const executor = new RecordingExecutor();
    await start(executor);
    server.send(opFrame("a"));
    await server.until(() => results("a").length === 1);
    server.send(opFrame("a"));
    await server.until(() => results("a").length === 2);
    expect(executor.ran).toEqual(["a"]);
    expect(results("a").map((f) => f.outcome)).toEqual([{ ok: "ran a" }, { ok: "ran a" }]);
  });

  it("does not run it twice while it runs", async () => {
    const executor = new RecordingExecutor(true);
    await start(executor);
    server.send(opFrame("a"));
    await server.until(() => executor.ran.length === 1);
    server.send(opFrame("a"));
    // Frames are read in order: once "z" runs, the repeat of "a" was read while "a" ran.
    server.send(opFrame("z"));
    await server.until(() => executor.ran.length === 2);
    expect(executor.ran).toEqual(["a", "z"]);
    executor.finish("a");
    await server.until(() => results("a").length === 1);
  });
});

describe("a cancel", () => {
  it("stops a running operation, and nothing is sent for it", async () => {
    const executor = new RecordingExecutor(true);
    await start(executor);
    server.send(opFrame("a"));
    await server.until(() => executor.ran.length === 1);
    server.send({ type: "cancel", id: "a" });
    await server.until(() => executor.aborted.length === 1);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(results("a")).toEqual([]);
  });

  it("that came first means the operation never runs", async () => {
    const executor = new RecordingExecutor();
    await start(executor);
    server.send({ type: "cancel", id: "a" });
    server.send(opFrame("a"));
    server.send(opFrame("z"));
    await server.until(() => executor.ran.length === 1);
    expect(executor.ran).toEqual(["z"]);
  });
});

describe("a result too large for one frame", () => {
  it("is answered too_large, so the server never has to refuse it", async () => {
    const executor: Executor = { run: () => Promise.resolve({ ok: "x".repeat(MAX_FRAME_CHARS) }) };
    await start(executor);
    server.send(opFrame("a"));
    await server.until(() => results("a").length === 1);
    expect(results("a")[0]?.outcome).toEqual(TOO_LARGE);
  });

  it("says the operation ran, so the agent checks before it repeats an effectful command", () => {
    expect(TOO_LARGE).toMatchObject({
      error: { type: "too_large", message: expect.stringMatching(/ran.*before repeating/i) },
    });
  });
});

describe("a failure too large for one frame", () => {
  it("from an executor that rejects is answered too_large, not journaled as an oversized frame", async () => {
    const { journal } = await start({ run: () => Promise.reject(new Error("x".repeat(MAX_FRAME_CHARS))) });
    server.send(opFrame("a"));
    await server.until(() => results("a").length === 1);
    expect(results("a")[0]?.outcome).toEqual(TOO_LARGE);
    expect(accepted(results("a")[0])).toBe(true);
    expect(journal.unsent().map((row) => row.outcome)).toEqual([TOO_LARGE]);
  });
});

describe("the link coming and going", () => {
  it("sends a result produced while offline once it is back, and reports what it still holds", async () => {
    const executor = new RecordingExecutor(true);
    await start(executor);
    server.send(opFrame("a"));
    server.send(opFrame("b"));
    await server.until(() => executor.ran.length === 2);
    server.close(1011);
    await server.until(() => link?.status !== "connected");
    executor.finish("a");
    await server.until(() => server.connections === 2 && link?.status === "connected");
    await server.until(() => results("a").length === 1);
    expect(server.hellos.at(-1)?.open).toEqual(["b"]);
  });

  it("answers an operation cut off by a crash as interrupted, and holds nothing open", async () => {
    const path = join(dir, "journal.sqlite");
    const before = new OperationJournal(path);
    before.receive({
      id: "a", sessionId: "r", callingSessionId: "r", invocationId: "1:c", ordinal: 1,
      kind: "run", args: {}, digest: "digest-a",
    });
    before.start("a");
    before.close();

    const executor = new RecordingExecutor();
    await start(executor, open(path));
    await server.until(() => results("a").length === 1);
    expect(results("a")[0]?.outcome).toEqual(INTERRUPTED);
    expect(server.hellos[0]?.open).toEqual([]);
    server.send(opFrame("a", "run"));
    await server.until(() => results("a").length === 2);
    expect(executor.ran).toEqual([]);
  });

  it("stops resending a result once the server acknowledged it", async () => {
    const executor = new RecordingExecutor();
    const { journal } = await start(executor);
    server.send(opFrame("a"));
    await server.until(() => results("a").length === 1);
    server.send({ type: "op_ack", id: "a" });
    await server.until(() => journal.unsent().length === 0);
    server.close(1011);
    await server.until(() => server.connections === 2 && link?.status === "connected");
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(results("a")).toHaveLength(1);
  });
});

describe("an outcome that is not an answer", () => {
  it.each([
    ["a BigInt", () => ({ ok: 1n })],
    ["a cycle", () => { const loop: Record<string, unknown> = {}; loop.self = loop; return { ok: loop }; }],
    ["a toJSON that throws", () => ({ ok: { toJSON: () => { throw new Error("no json"); } } })],
  ])("holding %s is answered as an error, and the next operation still runs", async (_name, outcome) => {
    const executor: Executor = {
      run: (operation) => Promise.resolve(operation.id === "a" ? (outcome() as Outcome) : { ok: "fine" }),
    };
    await start(executor);
    server.send(opFrame("a"));
    server.send(opFrame("b"));
    await server.until(() => results("a").length === 1 && results("b").length === 1);
    expect(results("a")[0]?.outcome).toMatchObject({ error: { type: "other" } });
    expect(results("b")[0]?.outcome).toEqual({ ok: "fine" });
  });

  it.each([
    ["an error that is text", () => ({ error: "x" })],
    ["an error with no message", () => ({ error: { type: "os" } })],
    ["a function", () => ({ ok: () => 1 })],
    ["a symbol", () => ({ ok: Symbol("s") })],
    ["both an ok and an error", () => ({ ok: 1, error: { type: "os", message: "x" } })],
    ["neither an ok nor an error", () => ({})],
    ["nothing", () => undefined],
    ["a list", () => [{ ok: 1 }]],
  ])("holding %s is answered with a frame the server accepts, journaled as sent", async (_name, outcome) => {
    const { journal } = await start({ run: () => Promise.resolve(outcome() as Outcome) });
    server.send(opFrame("a"));
    await server.until(() => results("a").length === 1);
    const frame = results("a")[0];
    expect(accepted(frame)).toBe(true);
    expect(frame?.outcome).toMatchObject({ error: { type: "other" } });
    expect(journal.unsent().map((row) => row.outcome)).toEqual([frame?.outcome]);
  });

  it.each([
    ["an ok of null", { ok: null }, { ok: null }],
    ["an ok of undefined", { ok: undefined }, { ok: null }],
    ["an error with details", { error: { type: "os", code: "ENOENT", message: "gone" } }, undefined],
  ])("holding %s is sent as it is", async (_name, outcome, sent) => {
    await start({ run: () => Promise.resolve(outcome as Outcome) });
    server.send(opFrame("a"));
    await server.until(() => results("a").length === 1);
    expect(results("a")[0]?.outcome).toEqual(sent ?? outcome);
  });

  it.each([
    ["throws", () => { throw new Error("boom"); }],
    ["rejects", () => Promise.reject(new Error("boom"))],
  ])("from an executor that %s is answered as an error", async (_name, run) => {
    await start({ run } as Executor);
    server.send(opFrame("a"));
    await server.until(() => results("a").length === 1);
    expect(results("a")[0]?.outcome).toEqual({ error: { type: "other", message: "boom" } });
  });
});

describe("a journal that fails", () => {
  it("while recording an outcome is reported, stops the link, and leaves the operation open", async () => {
    const failure = new Error("disk full");
    const errors: unknown[] = [];
    const { journal } = await start(new RecordingExecutor(), open(), (error) => errors.push(error));
    vi.spyOn(journal, "finish").mockImplementation(() => {
      throw failure;
    });
    server.send(opFrame("a"));
    await server.until(() => link?.status === "stopped");
    expect(errors).toEqual([failure]);
    expect(journal.openIds()).toEqual(["a"]);
  });

  it("while receiving an operation is reported, and stops the link without reconnecting", async () => {
    const failure = new Error("disk full");
    const errors: unknown[] = [];
    const { journal } = await start(new RecordingExecutor(), open(), (error) => errors.push(error));
    vi.spyOn(journal, "receive").mockImplementation(() => {
      throw failure;
    });
    server.send(opFrame("a"));
    await server.until(() => link?.status === "stopped");
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(errors).toEqual([failure]);
    expect(server.connections).toBe(1);
  });
});

describe("a journal that belongs to another device", () => {
  it("stops the link and touches nothing, even what is sent right behind the welcome", async () => {
    const journal = open();
    expect(journal.claim("another-device")).toBe(true);
    // An outcome that device has not had acknowledged.
    journal.receive({
      id: "f", sessionId: "r", callingSessionId: "r", invocationId: "1:c", ordinal: 1,
      kind: "run", args: {}, digest: "digest-f",
    });
    journal.start("f");
    journal.finish("f", { ok: 1 });

    const raw = new WebSocketServer({ host: "127.0.0.1", port: 0 });
    await once(raw, "listening");
    let connections = 0;
    raw.on("connection", (socket) => {
      connections += 1;
      socket.once("message", () => {
        socket.send(JSON.stringify({
          type: "welcome", protocol: 1, device_id: "d", org_id: "o", agent_id: "a",
          user_id: "u", name: "Laptop", heartbeat_s: 15,
        }));
        socket.send(JSON.stringify(opFrame("a")));
        socket.send(JSON.stringify({ type: "cancel", id: "c" }));
        socket.send(JSON.stringify({ type: "op_ack", id: "f" }));
      });
    });
    try {
      const executor = new RecordingExecutor();
      const url = `ws://127.0.0.1:${(raw.address() as AddressInfo).port}`;
      const device = connectDevice({ url, token: "surg_dev_test", journal, executor, delay: () => 20 });
      link = device.link;
      device.link.start();
      await server.until(() => device.link.status === "stopped");
      await new Promise((resolve) => setTimeout(resolve, 100));
      expect(connections).toBe(1);
      expect(executor.ran).toEqual([]);
      expect(journal.openIds()).toEqual([]);
      // The cancel and the acknowledgement were not applied either.
      expect(journal.unsent().map((result) => result.id)).toEqual(["f"]);
      expect(journal.receive({
        id: "c", sessionId: "r", callingSessionId: "r", invocationId: "1:c", ordinal: 1,
        kind: "run", args: {}, digest: "digest-c",
      })).toEqual({ action: "run" });
    } finally {
      for (const client of raw.clients) client.terminate();
      await new Promise<void>((resolve) => raw.close(() => resolve()));
    }
  });
});

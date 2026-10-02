import { once } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";
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

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), "runner-"));
  server = new FakeLinkServer();
  link = null;
});

afterEach(async () => {
  await link?.stop();
  await server.stop();
  rmSync(dir, { recursive: true, force: true });
});

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

async function start(executor: Executor, journal = new OperationJournal(join(dir, "journal.sqlite"))) {
  const url = await server.start();
  const device = connectDevice({ url, token: "surg_dev_test", journal, executor, delay: () => 20 });
  link = device.link;
  device.link.start();
  await server.until(() => device.link.status === "connected");
  return device;
}

const results = (id: string) => server.received.filter((f) => f.type === "op_result" && f.id === id);

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
    await start(executor, new OperationJournal(path));
    await server.until(() => results("a").length === 1);
    expect(results("a")[0]?.outcome).toEqual(INTERRUPTED);
    expect(server.hellos[0]?.open).toEqual([]);
    server.send(opFrame("a", "run"));
    await server.until(() => results("a").length === 2);
    expect(executor.ran).toEqual([]);
  });

  it("stops resending a result once the server acknowledged it", async () => {
    const executor = new RecordingExecutor();
    const journal = new OperationJournal(join(dir, "journal.sqlite"));
    await start(executor, journal);
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

describe("a journal that belongs to another device", () => {
  it("stops the link and runs nothing, even an operation sent right behind the welcome", async () => {
    const journal = new OperationJournal(join(dir, "journal.sqlite"));
    expect(journal.claim("another-device")).toBe(true);
    const raw = new WebSocketServer({ host: "127.0.0.1", port: 0 });
    await once(raw, "listening");
    raw.on("connection", (socket) => {
      socket.once("message", () => {
        socket.send(JSON.stringify({
          type: "welcome", protocol: 1, device_id: "d", org_id: "o", agent_id: "a",
          user_id: "u", name: "Laptop", heartbeat_s: 15,
        }));
        socket.send(JSON.stringify(opFrame("a")));
      });
    });
    try {
      const executor = new RecordingExecutor();
      const url = `ws://127.0.0.1:${(raw.address() as AddressInfo).port}`;
      const device = connectDevice({ url, token: "surg_dev_test", journal, executor, delay: () => 20 });
      link = device.link;
      device.link.start();
      await server.until(() => device.link.status === "stopped");
      expect(executor.ran).toEqual([]);
      expect(journal.openIds()).toEqual([]);
    } finally {
      for (const client of raw.clients) client.terminate();
      await new Promise<void>((resolve) => raw.close(() => resolve()));
    }
  });
});

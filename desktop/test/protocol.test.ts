import { describe, expect, it } from "vitest";

import { reconnectDelayMs } from "../src/link/backoff.js";
import { chunkAck, chunkFrame, hello, opResult, parseServerFrame, ProtocolError, transferOf } from "../src/link/protocol.js";

const welcome = {
  type: "welcome", protocol: 1, device_id: "d", org_id: "o", agent_id: "a", user_id: "u",
  name: "Laptop", heartbeat_s: 15,
};

const op = {
  type: "op",
  id: "7d0c3d2e-0000-4000-8000-000000000001",
  session_id: "s-root",
  calling_session_id: "s-call",
  invocation_id: "12:call_1",
  ordinal: 1,
  kind: "which",
  args: { name: "sh" },
  digest: "abc",
};

describe("server frames", () => {
  it("reads a welcome", () => {
    const frame = parseServerFrame(JSON.stringify(welcome));
    expect(frame).toEqual({
      type: "welcome",
      welcome: { deviceId: "d", orgId: "o", agentId: "a", userId: "u", name: "Laptop", heartbeatS: 15 },
    });
  });

  it("reads an operation", () => {
    expect(parseServerFrame(JSON.stringify(op))).toEqual({
      type: "op",
      operation: {
        id: op.id, sessionId: "s-root", callingSessionId: "s-call", invocationId: "12:call_1",
        ordinal: 1, kind: "which", args: { name: "sh" }, digest: "abc",
      },
    });
  });

  it.each([
    ["pong", { type: "pong" }],
    ["op_ack", { type: "op_ack", id: "x" }],
    ["cancel", { type: "cancel", id: "x" }],
    ["error", { type: "error", code: "unsupported_protocol", supported: [1] }],
    ["unwanted", { type: "unwanted", id: "x" }],
    ["rejected", { type: "rejected", id: "x" }],
  ])("reads a %s", (_name, frame) => {
    expect(parseServerFrame(JSON.stringify(frame)).type).toBe(frame.type);
  });

  it("reads a chunk's acknowledgement", () => {
    expect(parseServerFrame(JSON.stringify({ type: "chunk_ack", id: "x", seq: 3 }))).toEqual({
      type: "chunk_ack", id: "x", seq: 3,
    });
  });

  it("reads a chunk of a write's data, decoded", () => {
    expect(parseServerFrame(JSON.stringify({ type: "chunk", id: "x", seq: 2, data: "+/8=" }))).toEqual({
      type: "chunk", id: "x", seq: 2, data: Buffer.from([0xfb, 0xff]),
    });
  });

  it("ignores a frame type it does not know", () => {
    expect(parseServerFrame(JSON.stringify({ type: "surprise" }))).toEqual({ type: "unknown" });
  });

  it.each([
    ["not JSON", "{"],
    ["not an object", "[1]"],
    ["null", "null"],
    ["a number", "42"],
    ["a string", JSON.stringify("text")],
    ["a frame without a type", JSON.stringify({ id: "x" })],
    ["an operation without args", JSON.stringify({ ...op, args: undefined })],
    ["an operation with a fractional ordinal", JSON.stringify({ ...op, ordinal: 1.5 })],
    ["a welcome without a heartbeat", JSON.stringify({ ...welcome, heartbeat_s: undefined })],
    ["a cancel without an id", JSON.stringify({ type: "cancel" })],
    ["an unwanted without an id", JSON.stringify({ type: "unwanted" })],
    ["a rejected without an id", JSON.stringify({ type: "rejected" })],
    ["a chunk_ack without a seq", JSON.stringify({ type: "chunk_ack", id: "x" })],
    ["a chunk_ack with a fractional seq", JSON.stringify({ type: "chunk_ack", id: "x", seq: 0.5 })],
    ["a chunk_ack with a negative seq", JSON.stringify({ type: "chunk_ack", id: "x", seq: -1 })],
    ["a chunk without data", JSON.stringify({ type: "chunk", id: "x", seq: 0 })],
    ["a chunk with a fractional seq", JSON.stringify({ type: "chunk", id: "x", seq: 0.5, data: "AAAA" })],
    ["a chunk whose data is not padded", JSON.stringify({ type: "chunk", id: "x", seq: 0, data: "+/8" })],
    ["a chunk whose data is base64url", JSON.stringify({ type: "chunk", id: "x", seq: 0, data: "-_8=" })],
    ["a chunk whose data has a line break", JSON.stringify({ type: "chunk", id: "x", seq: 0, data: "AAAA\nAAAA" })],
  ])("refuses %s", (_name, text) => {
    expect(() => parseServerFrame(text)).toThrow(ProtocolError);
  });
});

describe("a welcome's heartbeat", () => {
  // JSON.stringify cannot write 1e999, so the value goes in as raw text.
  const withHeartbeat = (raw: string) =>
    JSON.stringify({ ...welcome, heartbeat_s: "HB" }).replace('"HB"', raw);

  it.each(["1e999", "0", "-1", "0.5", "301", '"15"'])("refuses %s", (raw) => {
    expect(() => parseServerFrame(withHeartbeat(raw))).toThrow(ProtocolError);
  });

  it.each(["1", "15", "300"])("accepts %s", (raw) => {
    expect(parseServerFrame(withHeartbeat(raw)).type).toBe("welcome");
  });
});

describe("app frames", () => {
  it("says hello with the protocol and what it holds unfinished", () => {
    expect(hello(["a", "b"])).toEqual({ type: "hello", protocols: [1], open: ["a", "b"] });
  });

  it("answers an operation by id and digest", () => {
    expect(opResult({ id: "x", digest: "d" }, { ok: null })).toEqual({
      type: "op_result", id: "x", digest: "d", outcome: { ok: null },
    });
  });
});

describe("a transfer", () => {
  it("is what a result names in place of a read's data", () => {
    const transfer = { size: 5_000_000, sha256: "f".repeat(64) };
    expect(transferOf({ ok: { transfer } })).toEqual(transfer);
    expect(transferOf({ ok: "ZGF0YQ==" })).toBeNull();
    expect(transferOf({ ok: null })).toBeNull();
    expect(transferOf({ error: { type: "os", message: "gone" } })).toBeNull();
  });

  it("sends its data in numbered chunks of standard base64", () => {
    expect(chunkFrame("x", 2, Buffer.from([0xfb, 0xff]))).toEqual({ type: "chunk", id: "x", seq: 2, data: "+/8=" });
  });

  it("acknowledges each chunk of a write's data by its id and seq", () => {
    expect(chunkAck("x", 2)).toEqual({ type: "chunk_ack", id: "x", seq: 2 });
  });
});

describe("an operation's result", () => {
  it("sends null for an ok that is undefined, which JSON would drop", () => {
    const frame = opResult({ id: "x", digest: "d" }, { ok: undefined });
    expect(JSON.parse(JSON.stringify(frame))).toEqual({
      type: "op_result", id: "x", digest: "d", outcome: { ok: null },
    });
  });
});

describe("reconnect delays", () => {
  it("grow from a second to at most a minute, with jitter that never halves below", () => {
    expect(reconnectDelayMs(0, () => 0)).toBe(500);
    expect(reconnectDelayMs(0, () => 1)).toBe(1000);
    expect(reconnectDelayMs(3, () => 1)).toBe(8000);
    expect(reconnectDelayMs(20, () => 1)).toBe(60_000);
    expect(reconnectDelayMs(20, () => 0)).toBe(30_000);
  });
});

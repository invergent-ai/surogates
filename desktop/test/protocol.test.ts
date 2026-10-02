import { describe, expect, it } from "vitest";

import { reconnectDelayMs } from "../src/link/backoff.js";
import { hello, opResult, parseServerFrame, ProtocolError } from "../src/link/protocol.js";

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
    const frame = parseServerFrame(JSON.stringify({
      type: "welcome", protocol: 1, device_id: "d", org_id: "o", agent_id: "a", user_id: "u",
      name: "Laptop", heartbeat_s: 15,
    }));
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
  ])("reads a %s", (_name, frame) => {
    expect(parseServerFrame(JSON.stringify(frame)).type).toBe(frame.type);
  });

  it("ignores a frame type it does not know", () => {
    expect(parseServerFrame(JSON.stringify({ type: "surprise" }))).toEqual({ type: "unknown" });
  });

  it.each([
    ["not JSON", "{"],
    ["not an object", "[1]"],
    ["a frame without a type", JSON.stringify({ id: "x" })],
    ["an operation without args", JSON.stringify({ ...op, args: undefined })],
    ["an operation with a fractional ordinal", JSON.stringify({ ...op, ordinal: 1.5 })],
    ["a welcome without a heartbeat", JSON.stringify({ type: "welcome", protocol: 1 })],
    ["a cancel without an id", JSON.stringify({ type: "cancel" })],
  ])("refuses %s", (_name, text) => {
    expect(() => parseServerFrame(text)).toThrow(ProtocolError);
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

describe("reconnect delays", () => {
  it("grow from a second to at most a minute, with jitter that never halves below", () => {
    expect(reconnectDelayMs(0, () => 0)).toBe(500);
    expect(reconnectDelayMs(0, () => 1)).toBe(1000);
    expect(reconnectDelayMs(3, () => 1)).toBe(8000);
    expect(reconnectDelayMs(20, () => 1)).toBe(60_000);
    expect(reconnectDelayMs(20, () => 0)).toBe(30_000);
  });
});

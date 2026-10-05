// The device link's frames, version 1. The contract is the module docstring
// of surogates/devices/link.py; this module only reads and writes it.

export const PROTOCOL_VERSION = 1;
// No welcome this long after connecting: drop the connection and reconnect.
export const WELCOME_TIMEOUT_MS = 10_000;
// The largest frame either side sends: one operation's 1 MiB of file data,
// base64-encoded, plus its envelope.
export const MAX_FRAME_CHARS = 2 * 1024 * 1024;
// A transfer's chunks carry this much of its data each, the last one the rest.
export const CHUNK_BYTES = 1024 * 1024;
// The most chunks sent ahead of the server's acknowledgements.
export const TRANSFER_WINDOW = 4;
// The longest heartbeat a welcome may ask for (the shortest is 1 s): a value
// outside that makes the timers never fire or fire every millisecond.
export const MAX_HEARTBEAT_S = 300;

export const Close = {
  protocol: 4400,
  unauthenticated: 4401,
  revoked: 4403,
  idle: 4408,
  superseded: 4409,
} as const;

export type Outcome =
  | { ok: unknown }
  | { error: { type: string; message: string; [detail: string]: unknown } };

// What a read's result or a write's args name in place of data too large for one
// frame: the data follows in chunks. sha256 is the data's, in lowercase hex.
export interface Transfer {
  size: number;
  sha256: string;
}

export interface Operation {
  id: string;
  sessionId: string;
  callingSessionId: string;
  invocationId: string;
  ordinal: number;
  kind: string;
  args: Record<string, unknown>;
  digest: string;
}

export interface Welcome {
  deviceId: string;
  orgId: string;
  agentId: string;
  userId: string;
  name: string;
  heartbeatS: number;
}

export type ServerFrame =
  | { type: "welcome"; welcome: Welcome }
  | { type: "pong" }
  | { type: "op"; operation: Operation }
  | { type: "op_ack"; id: string }
  | { type: "cancel"; id: string }
  | { type: "chunk_ack"; id: string; seq: number }
  // A chunk of a write's data, after its op: CHUNK_BYTES of it, the last chunk the rest.
  | { type: "chunk"; id: string; seq: number; data: Buffer }
  // The server closed the operation: stop sending its transfer; the result counts as acknowledged.
  | { type: "unwanted"; id: string }
  | { type: "error"; code: string; supported: number[] }
  // A frame type this app does not know: ignored, so a newer server's
  // additions within protocol 1 do not end the link.
  | { type: "unknown" };

export class ProtocolError extends Error {}

/**
 * Whether *text* is standard padded base64, as surogates/devices/workspace.py requires
 * data to be. One character class and no repeated group, which would overflow the regex
 * engine's stack on megabytes of text: this never grows a stack, and scans in linear
 * time, twice on a bad tail.
 */
export function isBase64(text: string): boolean {
  return text.length % 4 === 0 && /^[A-Za-z0-9+/]*={0,2}$/.test(text);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(frame: Record<string, unknown>, key: string): string {
  const value = frame[key];
  if (typeof value !== "string" || value === "") {
    throw new ProtocolError(`${String(frame.type)} frame needs a ${key}`);
  }
  return value;
}

export function parseServerFrame(raw: string): ServerFrame {
  let frame: unknown;
  try {
    frame = JSON.parse(raw);
  } catch {
    throw new ProtocolError("a frame is JSON");
  }
  if (!isObject(frame)) {
    throw new ProtocolError("a frame is a JSON object");
  }
  switch (frame.type) {
    case "welcome": {
      const heartbeat = frame.heartbeat_s;
      if (
        typeof heartbeat !== "number" || !Number.isFinite(heartbeat)
        || heartbeat < 1 || heartbeat > MAX_HEARTBEAT_S
      ) {
        throw new ProtocolError(`welcome frame needs a heartbeat_s from 1 to ${MAX_HEARTBEAT_S}`);
      }
      return {
        type: "welcome",
        welcome: {
          deviceId: text(frame, "device_id"),
          orgId: text(frame, "org_id"),
          agentId: text(frame, "agent_id"),
          userId: text(frame, "user_id"),
          name: text(frame, "name"),
          heartbeatS: heartbeat,
        },
      };
    }
    case "pong":
      return { type: "pong" };
    case "op": {
      const ordinal = frame.ordinal;
      if (typeof ordinal !== "number" || !Number.isInteger(ordinal) || ordinal < 0) {
        throw new ProtocolError("op frame needs a whole ordinal");
      }
      if (!isObject(frame.args)) {
        throw new ProtocolError("op frame needs args");
      }
      return {
        type: "op",
        operation: {
          id: text(frame, "id"),
          sessionId: text(frame, "session_id"),
          callingSessionId: text(frame, "calling_session_id"),
          invocationId: text(frame, "invocation_id"),
          ordinal,
          kind: text(frame, "kind"),
          args: frame.args,
          digest: text(frame, "digest"),
        },
      };
    }
    case "op_ack":
      return { type: "op_ack", id: text(frame, "id") };
    case "cancel":
      return { type: "cancel", id: text(frame, "id") };
    case "chunk_ack": {
      const seq = frame.seq;
      if (typeof seq !== "number" || !Number.isInteger(seq) || seq < 0) {
        throw new ProtocolError("chunk_ack frame needs a whole seq");
      }
      return { type: "chunk_ack", id: text(frame, "id"), seq };
    }
    case "unwanted":
      return { type: "unwanted", id: text(frame, "id") };
    case "chunk": {
      const { seq, data } = frame;
      if (typeof seq !== "number" || !Number.isInteger(seq) || seq < 0) {
        throw new ProtocolError("chunk frame needs a whole seq");
      }
      // Strict: a lenient decode drops what it does not know, and the write would land changed.
      if (typeof data !== "string" || !isBase64(data)) {
        throw new ProtocolError("chunk frame needs data in standard padded base64");
      }
      return { type: "chunk", id: text(frame, "id"), seq, data: Buffer.from(data, "base64") };
    }
    case "error": {
      const supported = Array.isArray(frame.supported)
        ? frame.supported.filter((v): v is number => typeof v === "number")
        : [];
      return { type: "error", code: text(frame, "code"), supported };
    }
    default:
      if (typeof frame.type !== "string") throw new ProtocolError("a frame has a type");
      return { type: "unknown" };
  }
}

export function hello(open: string[]): Record<string, unknown> {
  return { type: "hello", protocols: [PROTOCOL_VERSION], open };
}

export function opResult(
  operation: { id: string; digest: string },
  outcome: Outcome,
): Record<string, unknown> {
  // JSON.stringify drops an undefined ok, a frame the server refuses: send null.
  const sent = "ok" in outcome && outcome.ok === undefined ? { ok: null } : outcome;
  return { type: "op_result", id: operation.id, digest: operation.digest, outcome: sent };
}

/** The transfer a result names in place of a read's data, or null. */
export function transferOf(outcome: Outcome): Transfer | null {
  if (!("ok" in outcome) || !isObject(outcome.ok) || !isObject(outcome.ok.transfer)) return null;
  return outcome.ok.transfer as unknown as Transfer;
}

export function chunkFrame(id: string, seq: number, data: Buffer): Record<string, unknown> {
  return { type: "chunk", id, seq, data: data.toString("base64") };
}

export function chunkAck(id: string, seq: number): Record<string, unknown> {
  return { type: "chunk_ack", id, seq };
}

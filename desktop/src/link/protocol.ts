// The device link's frames, version 1. The contract is the module docstring
// of surogates/devices/link.py; this module only reads and writes it.

export const PROTOCOL_VERSION = 1;
// No welcome this long after connecting: drop the connection and reconnect.
export const WELCOME_TIMEOUT_MS = 10_000;
// The largest frame either side sends: one operation's 1 MiB of file data,
// base64-encoded, plus its envelope.
export const MAX_FRAME_CHARS = 2 * 1024 * 1024;

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
  | { type: "error"; code: string; supported: number[] }
  // A frame type this app does not know: ignored, so a newer server's
  // additions within protocol 1 do not end the link.
  | { type: "unknown" };

export class ProtocolError extends Error {}

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
      if (typeof heartbeat !== "number" || !(heartbeat > 0)) {
        throw new ProtocolError("welcome frame needs a positive heartbeat_s");
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
  return { type: "op_result", id: operation.id, digest: operation.digest, outcome };
}

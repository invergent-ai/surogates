// The agent's side of the control port (protocol.ts, ToAgent and FromAgent): it
// says hello, then answers each of the host's requests once, by its id. The host
// runs the VM, so a line that is not a request is ignored; a request whose fields
// are not what its type names is the host's bug, and is answered, never thrown.

import type { Outcome } from "../link/protocol.js";
import type { ProcessHandle } from "./processes.js";
import { type FromAgent, type HostUser, MAX_PROTECTED, type ProtectedKey, type Share, type ToAgent } from "./protocol.js";

export const NO_HELLO = "The host has not answered hello";

// What the control asks of the roots (root.ts, Roots).
export interface ControlRoots {
  uid(root: string): number;
  setup(root: string, folder: string, share: Share, user: HostUser, ended: ProcessHandle[]): Promise<void>;
  teardown(root: string, share: Share): Promise<void>;
  // Rejects with the refusal for the root's commands.
  protect(root: string, keys: ProtectedKey[]): Promise<void>;
  // Never rejects: whatever goes wrong is an outcome.
  perform(root: string, kind: string, args: Record<string, unknown>, signal: AbortSignal, id: string): Promise<Outcome>;
}

const describe = (error: unknown) => (error instanceof Error ? error.message : String(error));
const malformed = (type: string) => `The agent cannot take this ${type} request`;
const isText = (value: unknown): value is string => typeof value === "string";
const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isKeys = (value: unknown): value is ProtectedKey[] => Array.isArray(value) && value.length <= MAX_PROTECTED &&
  value.every((key) => Array.isArray(key) && key.length === 3 && isText(key[0]) && Number.isInteger(key[1]) && (key[2] === "ro" || key[2] === "rw"));

// The kinds of share this agent mounts.
const isShare = (value: unknown): value is Share => isRecord(value) && value.kind === "virtiofs" && isText(value.tag);

function isUser(value: unknown): value is HostUser {
  if (!isRecord(value)) return false;
  const { uid, gid, name, home } = value;
  return Number.isInteger(uid) && Number.isInteger(gid) && isText(name) && isText(home);
}

export class Control {
  private user: HostUser | null = null;
  // The operations still running, by id.
  private readonly running = new Map<number, AbortController>();

  constructor(private readonly send: (message: FromAgent) => void, private readonly roots: ControlRoots) {}

  hello(): void {
    this.send({ type: "hello", id: 0 });
  }

  receive(line: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      return;
    }
    if (!isRecord(parsed) || typeof parsed.id !== "number") return;
    const message = parsed as ToAgent & Record<string, unknown>;
    const { id } = message;
    if (message.type === "done") {
      // Hello's answer, once: an answer to a request of the agent's names no user.
      if (id === 0 && !this.user && isUser(message.user)) this.user = message.user;
    } else if (message.type === "ping") {
      this.send({ type: "pong", id });
    } else if (message.type === "uid") {
      if (!isText(message.root)) return this.send({ type: "failed", id, message: malformed("uid") });
      try {
        this.send({ type: "done", id, uid: this.roots.uid(message.root) });
      } catch (error) {
        this.send({ type: "failed", id, message: describe(error) });
      }
    } else if (message.type === "setup") {
      if (![message.root, message.folder].every(isText) || !isShare(message.share) || !Array.isArray(message.ended)) {
        return this.send({ type: "failed", id, message: malformed("setup") });
      }
      if (!this.user) return this.send({ type: "failed", id, message: NO_HELLO });
      this.roots.setup(message.root, message.folder, message.share, this.user, message.ended).then(
        () => this.send({ type: "done", id }),
        (error: unknown) => this.send({ type: "failed", id, message: describe(error) }),
      );
    } else if (message.type === "op") {
      if (!isText(message.root) || !isText(message.kind)) {
        return this.send({ type: "result", id, outcome: { error: { type: "value", message: malformed("op") } } });
      }
      // The host numbers its own requests: one still running keeps its id, and its cancel.
      if (this.running.has(id)) {
        return this.send({ type: "result", id, outcome: { error: { type: "other", message: "An operation with this id is already running" } } });
      }
      const controller = new AbortController();
      this.running.set(id, controller);
      void this.roots.perform(message.root, message.kind, isRecord(message.args) ? message.args : {}, controller.signal, `op-${id}`)
        .catch((error: unknown): Outcome => ({ error: { type: "other", message: String(error) } }))
        .then((outcome) => {
          this.running.delete(id);
          this.send({ type: "result", id, outcome });
        });
    } else if (message.type === "teardown") {
      if (!isText(message.root) || !isShare(message.share)) return this.send({ type: "failed", id, message: malformed("teardown") });
      this.roots.teardown(message.root, message.share).then(
        () => this.send({ type: "done", id }),
        (error: unknown) => this.send({ type: "failed", id, message: describe(error) }),
      );
    } else if (message.type === "protect") {
      if (!isText(message.root) || !isKeys(message.keys)) return this.send({ type: "failed", id, message: malformed("protect") });
      this.roots.protect(message.root, message.keys).then(
        () => this.send({ type: "done", id }),
        (error: unknown) => this.send({ type: "failed", id, message: describe(error) }),
      );
    } else if (message.type === "cancel") {
      this.running.get(id)?.abort();
    } else {
      this.send({ type: "failed", id, message: `The agent does not know the request ${String(parsed.type)}` });
    }
  }
}

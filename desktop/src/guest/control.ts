// The agent's side of the control port (protocol.ts, ToAgent and FromAgent): it
// says hello, then answers each of the host's requests once, by its id. The host
// runs the VM, so a line that is not a request is ignored; a request whose fields
// are not what its type names is the host's bug, and is answered, never thrown.

import type { Outcome } from "../link/protocol.js";
import type { ProcessHandle } from "./processes.js";
import type { FromAgent, HostUser, Share, ToAgent } from "./protocol.js";

export const NO_HELLO = "The host has not answered hello";
const NO_PLACES = "The agent keeps no folder's history";

// What the control asks of the roots (root.ts, Roots).
export interface ControlRoots {
  uid(root: string): number;
  setup(root: string, folder: string, share: Share, user: HostUser, ended: ProcessHandle[]): Promise<void>;
  teardown(root: string, share: Share): Promise<void>;
  // Never rejects: whatever goes wrong is an outcome.
  perform(root: string, kind: string, args: Record<string, unknown>, signal: AbortSignal, id: string): Promise<Outcome>;
}

// What the control asks of the folders' places (places.ts).
export interface ControlPlaces {
  mount(key: string, history: Share, real: Share): Promise<void>;
  unmount(key: string): Promise<void>;
  // Never rejects: whatever goes wrong is an outcome.
  history(key: string, request: { thread: string; user: string; action: string; args: Record<string, unknown> }, signal: AbortSignal): Promise<Outcome>;
}

// What the control asks of the guest itself (root.ts): its clock, its runs' backstops, and its power.
export interface ControlMachine {
  // The guest's clock set to *now*, milliseconds since the epoch.
  setClock(now: number): Promise<void>;
  // The computer slept *ms*: every run's backstop falls that much later (Roots.woke).
  woke(ms: number): void;
  // The host said something: a backstop that waited to hear it goes on (Roots.heard).
  heard(): void;
  // Every root's processes end, the sessions disk is written out and let go, and the guest powers off.
  powerOff(): Promise<void>;
}

const describe = (error: unknown) => (error instanceof Error ? error.message : String(error));
const malformed = (type: string) => `The agent cannot take this ${type} request`;
const isText = (value: unknown): value is string => typeof value === "string";
const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

// The kinds of share this agent mounts.
const isShare = (value: unknown): value is Share => isRecord(value) && value.kind === "virtiofs" && isText(value.tag);

function isUser(value: unknown): value is HostUser {
  if (!isRecord(value)) return false;
  const { uid, gid, name, home } = value;
  return Number.isInteger(uid) && Number.isInteger(gid) && isText(name) && isText(home);
}

export class Control {
  private user: HostUser | null = null;
  // The operations and the history requests still running, by id.
  private readonly running = new Map<number, AbortController>();

  // Without *machine*, as in the tests, the guest's clock and power are left alone; without *places*, no folder's history is mounted.
  constructor(
    private readonly send: (message: FromAgent) => void, private readonly roots: ControlRoots, private readonly machine?: ControlMachine,
    private readonly places?: ControlPlaces,
  ) {}

  hello(): void {
    this.send({ type: "hello", id: 0 });
  }

  // *work*, answered as the result of request *id*, and stopped by a cancel of that id.
  private answer(id: number, work: (signal: AbortSignal) => Promise<Outcome>): void {
    // The host numbers its own requests: one still running keeps its id, and its cancel.
    if (this.running.has(id)) {
      return this.send({ type: "result", id, outcome: { error: { type: "other", message: "An operation with this id is already running" } } });
    }
    const controller = new AbortController();
    this.running.set(id, controller);
    void work(controller.signal)
      .catch((error: unknown): Outcome => ({ error: { type: "other", message: String(error) } }))
      .then((outcome) => {
        this.running.delete(id);
        this.send({ type: "result", id, outcome });
      });
  }

  receive(line: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      return;
    }
    if (!isRecord(parsed) || typeof parsed.id !== "number") return;
    this.machine?.heard();
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
      const { root, kind } = message;
      const args = isRecord(message.args) ? message.args : {};
      this.answer(id, (signal) => this.roots.perform(root, kind, args, signal, `op-${id}`));
    } else if (message.type === "teardown") {
      if (!isText(message.root) || !isShare(message.share)) return this.send({ type: "failed", id, message: malformed("teardown") });
      this.roots.teardown(message.root, message.share).then(
        () => this.send({ type: "done", id }),
        (error: unknown) => this.send({ type: "failed", id, message: describe(error) }),
      );
    } else if (message.type === "history") {
      const { key, thread, user, action, args } = message;
      const { places } = this;
      if (![key, thread, user, action].every(isText) || !isRecord(args)) {
        return this.send({ type: "result", id, outcome: { error: { type: "value", message: malformed("history") } } });
      }
      if (!places) return this.send({ type: "result", id, outcome: { error: { type: "unavailable", message: NO_PLACES } } });
      this.answer(id, (signal) => places.history(key, { thread, user, action, args }, signal));
    } else if (message.type === "place" || message.type === "unplace") {
      const { type } = message;
      const shared = type === "unplace" || (isShare(message.history) && isShare(message.real));
      if (!isText(message.key) || !shared) return this.send({ type: "failed", id, message: malformed(type) });
      if (!this.places) return this.send({ type: "failed", id, message: NO_PLACES });
      (type === "place" ? this.places.mount(message.key, message.history, message.real) : this.places.unmount(message.key)).then(
        () => this.send({ type: "done", id }),
        (error: unknown) => this.send({ type: "failed", id, message: describe(error) }),
      );
    } else if (message.type === "time") {
      if (!Number.isFinite(message.now) || message.now <= 0 || !Number.isFinite(message.slept) || message.slept < 0) {
        return this.send({ type: "failed", id, message: malformed("time") });
      }
      this.machine?.woke(message.slept);
      (this.machine?.setClock(message.now) ?? Promise.resolve()).then(
        () => this.send({ type: "done", id }),
        (error: unknown) => this.send({ type: "failed", id, message: describe(error) }),
      );
    } else if (message.type === "shutdown") {
      // No answer: the guest powers off, which the host sees as the VM's exit.
      void this.machine?.powerOff().catch(() => {});
    } else if (message.type === "cancel") {
      this.running.get(id)?.abort();
    } else {
      this.send({ type: "failed", id, message: `The agent does not know the request ${String(parsed.type)}` });
    }
  }
}

// The agent's side of the control port (protocol.ts, ToAgent and FromAgent): it
// says hello, then answers each of the host's requests once, by its id. The host
// runs the VM, so its lines are trusted; one that is not a request is ignored.

import type { Outcome } from "../link/protocol.js";
import type { FromAgent, HostUser, ToAgent } from "./protocol.js";

export const NO_HELLO = "The host has not answered hello";

// What the control asks of the roots (root.ts, Roots).
export interface ControlRoots {
  uid(root: string): number;
  setup(root: string, folder: string, tag: string, user: HostUser): Promise<void>;
  perform(root: string, kind: string, args: Record<string, unknown>, signal: AbortSignal, id: string): Promise<Outcome>;
}

const describe = (error: unknown) => (error instanceof Error ? error.message : String(error));

export class Control {
  private user: HostUser | null = null;
  // The operations still running, by id.
  private readonly running = new Map<number, AbortController>();

  constructor(private readonly send: (message: FromAgent) => void, private readonly roots: ControlRoots) {}

  hello(): void {
    this.send({ type: "hello", id: 0 });
  }

  receive(line: string): void {
    let message: ToAgent;
    try {
      message = JSON.parse(line) as ToAgent;
    } catch {
      return;
    }
    if (typeof message !== "object" || message === null || typeof message.id !== "number") return;
    const { id } = message;
    if (message.type === "done") {
      // Only hello's: an answer to a request of the agent's names no user.
      if (id === 0) this.user = message.user;
    } else if (message.type === "ping") {
      this.send({ type: "pong", id });
    } else if (message.type === "uid") {
      try {
        this.send({ type: "done", id, uid: this.roots.uid(message.root) });
      } catch (error) {
        this.send({ type: "failed", id, message: describe(error) });
      }
    } else if (message.type === "setup") {
      if (!this.user) {
        this.send({ type: "failed", id, message: NO_HELLO });
        return;
      }
      this.roots.setup(message.root, message.folder, message.tag, this.user).then(
        () => this.send({ type: "done", id }),
        (error: unknown) => this.send({ type: "failed", id, message: describe(error) }),
      );
    } else if (message.type === "op") {
      const controller = new AbortController();
      this.running.set(id, controller);
      void this.roots.perform(message.root, message.kind, message.args ?? {}, controller.signal, `op-${id}`).then((outcome) => {
        this.running.delete(id);
        this.send({ type: "result", id, outcome });
      });
    } else if (message.type === "cancel") {
      this.running.get(id)?.abort();
    }
  }
}

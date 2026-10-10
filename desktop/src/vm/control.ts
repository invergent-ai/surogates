// The host's side of the guest's control port (spec, Section 11, Transport), on
// the byte stream the OS's backend opens for ai.surogate.control: on Linux, the
// Unix socket QEMU makes for it. The agent says hello and the host answers with its
// user; from then on the host asks, each request with an id of its own, and the
// agent answers each once (guest/protocol.ts).

import { createInterface } from "node:readline";
import type { Duplex } from "node:stream";

import { CANCELLED, SANDBOX_STOPPED } from "../guest/command.js";
import { MAX_PROCESSES, type ProcessHandle } from "../guest/processes.js";
import type { FromAgent, HostUser, ToAgent } from "../guest/protocol.js";
import { isHandle } from "../hosts/folder-record.js";
import type { Outcome } from "../link/protocol.js";
import { checked } from "./history.js";

// What the host asks: every message to the agent but hello's answer, its id added on sending.
type Without<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;
export type Request = Without<Exclude<ToAgent, { type: "done" }>, "id">;

// Past the agent's longest line, a result of MAX_MESSAGE_CHARS at about 4.5 MiB:
// a guest that sends one is not the agent, and the link closes as a lost VM's.
const MAX_LINE_BYTES = 8 * 1024 ** 2;

export class ControlLink {
  private next = 1;
  private readonly waiting = new Map<number, (answer: FromAgent | null) => void>();
  private ended = false;
  private lost: (root: string) => void = () => {};
  private handles: (root: string, handles: ProcessHandle[], live: number) => void = () => {};
  readonly closed: Promise<void>;

  private constructor(private readonly channel: Duplex, lines: AsyncIterableIterator<string>) {
    this.closed = (async () => {
      try {
        for await (const line of lines) this.received(line);
      } catch {
        // The channel failed: as closed.
      }
      this.ended = true;
      for (const answer of this.waiting.values()) answer(null);
      this.waiting.clear();
    })();
  }

  /**
   * The link on *channel*, once the agent has said hello on it and been told
   * *user*. Rejects at *deadline* (performance.now()), or when *gone* settles
   * first: the VM exited.
   */
  static async open(channel: Duplex, user: HostUser, deadline: number, gone: Promise<unknown>): Promise<ControlLink> {
    let exited = false;
    void gone.then(() => {
      exited = true;
    }, () => {
      exited = true;
    });
    const late = () => new Error(exited ? "The VM exited" : "The guest's agent did not say hello");
    channel.on("error", () => {});
    let unended = 0;
    channel.on("data", (chunk: Buffer) => {
      const newline = chunk.lastIndexOf(0x0a);
      unended = newline < 0 ? unended + chunk.length : chunk.length - newline - 1;
      if (unended > MAX_LINE_BYTES) channel.destroy();
    });
    const reader = createInterface({ input: channel, crlfDelay: Infinity });
    // A channel destroyed on this side ends with no 'end' for the reader to see.
    channel.once("close", () => reader.close());
    const lines = reader[Symbol.asyncIterator]();
    // Its first message is hello; a line before it that is not one is skipped. Only
    // the VM closes the channel: a guest that panics while it boots closes it before the VM's exit is seen.
    const hello = (async () => {
      for (;;) {
        const { value, done } = await lines.next();
        if (done) {
          exited = true;
          return false;
        }
        try {
          const message = JSON.parse(value) as FromAgent;
          if (message?.type === "hello") return true;
        } catch {
          // Not a message.
        }
      }
    })();
    let timer: NodeJS.Timeout | undefined;
    const said = await Promise.race([
      hello,
      gone.then(() => false, () => false),
      new Promise<false>((resolve) => {
        timer = setTimeout(() => resolve(false), Math.max(0, deadline - performance.now()));
      }),
    ]);
    clearTimeout(timer);
    if (!said) {
      channel.destroy();
      throw late();
    }
    const link = new ControlLink(channel, lines);
    link.write({ type: "done", id: 0, user });
    return link;
  }

  // The agent's answer, or null once the link has closed, or after *ms*.
  request(message: Request, ms?: number): Promise<FromAgent | null> {
    if (this.ended) return Promise.resolve(null);
    const id = this.next++;
    return new Promise((resolve) => {
      const timer = ms === undefined ? undefined : setTimeout(() => answer(null), ms);
      const answer = (reply: FromAgent | null) => {
        clearTimeout(timer);
        this.waiting.delete(id);
        resolve(reply);
      };
      this.waiting.set(id, answer);
      this.write({ ...message, id } as ToAgent);
    });
  }

  /** One operation of *root*'s. A cancel is answered at once; the agent is told, and its own answer goes unread. */
  op(root: string, kind: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome> {
    return this.cancellable({ type: "op", root, kind, args }, signal, (reply) => (reply.type === "result" ? reply.outcome : SANDBOX_STOPPED));
  }

  /**
   * One request to the history of the place *key*, for *thread*'s copy, cancelled as an operation is.
   * What the agent answered is the guest's: nobody has it before it is checked as the answer of
   * *action* (history.ts), and an answer that is no result at all is refused as one that is none.
   * A cancel, and a link that closed, are this computer's own to say.
   */
  history(key: string, thread: string, user: string, action: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome> {
    const asked: Request = { type: "history", key, thread, user, action, args };
    return this.cancellable(asked, signal, (reply) => checked(action, reply.type === "result" ? reply.outcome : undefined));
  }

  // *message*, cancelled by *signal*; its answer, as *read* takes it.
  private async cancellable(message: Request, signal: AbortSignal, read: (reply: FromAgent) => Outcome): Promise<Outcome> {
    if (signal.aborted) return CANCELLED;
    const id = this.next;
    let cancel = () => {};
    const cancelled = new Promise<"cancelled">((resolve) => {
      cancel = () => {
        this.write({ type: "cancel", id });
        resolve("cancelled");
      };
    });
    signal.addEventListener("abort", cancel, { once: true });
    const reply = await Promise.race([this.request(message), cancelled]);
    signal.removeEventListener("abort", cancel);
    if (reply === "cancelled") return CANCELLED;
    return reply ? read(reply) : SANDBOX_STOPPED;
  }

  // Told of each root whose runner the guest lost, and set up again by the next operation.
  onLost(listener: (root: string) => void): void {
    this.lost = listener;
  }

  // Told each change of a root's processes in the guest: the handles to keep, and how many live.
  onHandles(listener: (root: string, handles: ProcessHandle[], live: number) => void): void {
    this.handles = listener;
  }

  close(): void {
    this.channel.destroy();
  }

  private write(message: ToAgent): void {
    if (!this.ended) this.channel.write(`${JSON.stringify(message)}\n`);
  }

  // Kept in the folder's record and carried by each operation of the root's: at most a
  // registry's handles, each in a handle's shape and size. Others are the agent's bug,
  // refused whole, and the guest goes on.
  private handled({ root, handles, live }: Extract<FromAgent, { type: "handles" }>): void {
    if (Array.isArray(handles) && handles.length <= MAX_PROCESSES && handles.every(isHandle) && Number.isInteger(live)) {
      this.handles(root, handles, live);
    } else {
      console.warn(new Error(`The guest's process handles for ${JSON.stringify(root.slice(0, 64))} were refused: more than ${MAX_PROCESSES}, or not a handle's shape and size`));
    }
  }

  private received(line: string): void {
    let message: FromAgent;
    try {
      message = JSON.parse(line) as FromAgent;
    } catch {
      return;
    }
    if (message?.type === "lost" && typeof message.root === "string") this.lost(message.root);
    else if (message?.type === "handles" && typeof message.root === "string") this.handled(message);
    else if (typeof (message as { id?: unknown } | null)?.id === "number") this.waiting.get((message as { id: number }).id)?.(message);
  }
}

// Runs the operations the server sends, each at most once: the journal says
// whether one is new, already answered, cancelled or still running; the
// executor does the work. A cancelled operation is stopped and answered with
// nothing; an outcome that could not be sent is sent again after a reconnect.

import type { OperationJournal } from "../journal/journal.js";
import { MAX_FRAME_CHARS, opResult, type Operation, type Outcome } from "../link/protocol.js";

// The answer to an operation whose result would not fit one frame: the server
// would refuse that frame, and the result would be resent forever.
export const TOO_LARGE: Outcome = {
  error: { type: "too_large", message: "The computer's result is too large to send" },
};

export interface Executor {
  run(operation: Operation, signal: AbortSignal): Promise<Outcome>;
}

export class OperationRunner {
  private readonly running = new Map<string, AbortController>();

  constructor(
    private readonly journal: OperationJournal,
    private readonly executor: Executor,
    private readonly send: (frame: Record<string, unknown>) => boolean,
    // What failed after the executor answered (the journal, the link): the
    // operation stays "started", so the next launch answers it "interrupted".
    private readonly onError?: (error: unknown) => void,
  ) {}

  /** The link is back: send every outcome the server has not acknowledged. */
  connected(): void {
    for (const result of this.journal.unsent()) this.send(opResult(result, result.outcome));
  }

  operation(operation: Operation): void {
    const received = this.journal.receive(operation);
    if (received.action === "reply") {
      this.send(opResult(operation, received.outcome));
      return;
    }
    if (received.action === "ignore" || this.running.has(operation.id)) return;
    // The journal's claim decides: false if it was cancelled or started meanwhile.
    if (!this.journal.start(operation.id)) return;
    const controller = new AbortController();
    this.running.set(operation.id, controller);
    void this.execute(operation, controller.signal).catch((error: unknown) => this.onError?.(error));
  }

  cancel(id: string): void {
    this.journal.cancel(id);
    this.running.get(id)?.abort();
  }

  acknowledged(id: string): void {
    this.journal.acknowledge(id);
  }

  openIds(): string[] {
    return this.journal.openIds();
  }

  private async execute(operation: Operation, signal: AbortSignal): Promise<void> {
    let outcome: Outcome;
    try {
      outcome = await this.executor.run(operation, signal);
      // String length counts UTF-16 units, never fewer than the server counts. An
      // outcome that is not JSON (a BigInt, a cycle) fails here, and is answered below.
      if (JSON.stringify(opResult(operation, outcome)).length > MAX_FRAME_CHARS) outcome = TOO_LARGE;
    } catch (error) {
      outcome = { error: { type: "other", message: error instanceof Error ? error.message : String(error) } };
    } finally {
      this.running.delete(operation.id);
    }
    // A cancelled operation's late outcome is dropped: the server gave up on it.
    if (this.journal.finish(operation.id, outcome)) this.send(opResult(operation, outcome));
  }
}

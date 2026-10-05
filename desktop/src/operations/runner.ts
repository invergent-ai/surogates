// Runs the operations the server sends, each at most once: the journal says
// whether one is new, already answered, cancelled or still running; the
// executor does the work. A cancelled operation is stopped and answered with
// nothing; an outcome that could not be sent is sent again after a reconnect. A
// read's data too large for one frame goes as a transfer (transfers.ts).

import { createHash } from "node:crypto";

import { MAX_PAYLOAD_BYTES, MAX_READ_BYTES } from "../files/answers.js";
import type { OperationJournal } from "../journal/journal.js";
import { CHUNK_BYTES, MAX_FRAME_CHARS, opResult, type Operation, type Outcome, transferOf } from "../link/protocol.js";
import { report } from "../report.js";
import { type Send, TransferSender } from "./transfers.js";

// The answer to an operation whose result would not fit one frame: the server
// would refuse that frame, and the result would be resent forever. The operation
// did run, so the agent is told to look before it repeats one that has effects.
export const TOO_LARGE: Outcome = {
  error: {
    type: "too_large",
    message: "The operation ran, but its result is too large to send. Check what it did before repeating it.",
  },
};

// What a running operation is recorded as when the app stops it: it may have
// done part of its work, so the agent is told to look before it repeats it.
export const APP_CLOSED: Outcome = {
  error: { type: "interrupted", message: "interrupted: the app was closed while this ran. Check what it did before repeating it." },
};
export const ACCESS_ENDED: Outcome = {
  error: {
    type: "interrupted",
    message: "interrupted: this computer's access to the agent ended while this ran. Check what it did before repeating it.",
  },
};

// The abort reason suspend() gives: the outcome to record, whatever the executor answers.
class Suspension {
  constructor(readonly outcome: Outcome) {}
}

// What a read is answered when its chunks cannot be journaled: up to 50 MiB makes
// a full disk likely, and a journal error would stop the link for every chat.
const NO_SPACE: Outcome = {
  error: { type: "os", code: "ENOSPC", message: "Not enough free disk space on this computer to send this file" },
};
const NOT_KEPT: Outcome = {
  error: { type: "os", code: "EIO", message: "This computer could not keep this file to send it" },
};
// SQLite's result code for a full disk, as node:sqlite reports it in errcode.
const SQLITE_FULL = 13;

// An answer given before the operation started that could not be sent: nothing ran.
const ANSWER_TOO_LARGE: Outcome = {
  error: {
    type: "too_large",
    message: "The computer could not answer this operation: its answer is too large to send. It did not run.",
  },
};

function unsendable(why: string, ran: boolean): Outcome {
  const message = ran
    ? `The operation ran, but its result could not be sent: ${why}`
    : `The computer could not answer this operation: ${why}. It did not run.`;
  return { error: { type: "other", message } };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// What goes in the journal and on the wire: an outcome the server records, and
// nothing it would refuse. A refused result closes the link with 4400, and the
// journal would send it again at every welcome, forever. So the outcome is
// round-tripped through JSON (what JSON drops or cannot hold is found here, not
// by the server), then needs exactly one of ok and error, an error that names
// its type and message, and a frame that fits (surogates/devices/link.py). *ran*
// says whether the operation ran, so the agent knows whether to check what it did.
function sendable(operation: Operation, outcome: unknown, ran: boolean): Outcome {
  let value: unknown;
  try {
    // opResult sends an undefined ok as null, which JSON would drop.
    const kept = isRecord(outcome) ? opResult(operation, outcome as Outcome).outcome : outcome;
    value = JSON.parse(JSON.stringify(kept)) as unknown;
  } catch (error) {
    return unsendable(error instanceof Error ? error.message : String(error), ran);
  }
  if (!isRecord(value) || ["ok", "error"].filter((key) => key in value).length !== 1) {
    return unsendable("it holds neither an ok nor an error, or both", ran);
  }
  if ("error" in value) {
    const error = value.error;
    if (!isRecord(error) || typeof error.type !== "string" || typeof error.message !== "string") {
      return unsendable("its error has no type and message", ran);
    }
  }
  // String length counts UTF-16 units, never fewer than the server counts.
  if (JSON.stringify(opResult(operation, value as Outcome)).length <= MAX_FRAME_CHARS) return value as Outcome;
  return ran ? TOO_LARGE : ANSWER_TOO_LARGE;
}

// A read's data over MAX_PAYLOAD_BYTES leaves its outcome: the outcome names it by
// size and SHA-256 (surogates/devices/link.py), and its chunks are journaled beside it.
function carried(operation: Operation, outcome: Outcome): { outcome: Outcome; chunks: Buffer[] } {
  const encoded = operation.kind === "read" && isRecord(outcome) && "ok" in outcome ? outcome.ok : undefined;
  if (typeof encoded !== "string") return { outcome, chunks: [] };
  const size = Buffer.byteLength(encoded, "base64");
  if (size <= MAX_PAYLOAD_BYTES) return { outcome, chunks: [] };
  // The file helper reads at most MAX_READ_BYTES. More, the server would refuse, and it would be resent forever.
  if (size > MAX_READ_BYTES) return { outcome: TOO_LARGE, chunks: [] };
  const data = Buffer.from(encoded, "base64");
  const chunks: Buffer[] = [];
  for (let at = 0; at < data.length; at += CHUNK_BYTES) chunks.push(data.subarray(at, at + CHUNK_BYTES));
  const sha256 = createHash("sha256").update(data).digest("hex");
  return { outcome: { ok: { transfer: { size: data.length, sha256 } } }, chunks };
}

export interface Executor {
  /**
   * Do the operation and answer it. A throw or a rejection is answered as an error.
   * An abort listener on `signal` must not throw: Node's EventTarget rethrows a
   * listener's exception on the next tick as an uncaught exception, which no
   * try/catch here or in the caller can contain.
   */
  run(operation: Operation, signal: AbortSignal): Promise<Outcome>;
  /**
   * Answer the operation without running it, or let it run (null). Asked before the
   * journal marks it started, so whatever this waits on survives an app restart: the
   * operation is still "received", and the server sends it again. It may therefore be
   * asked more than once for one operation. A throw or a rejection is answered as an
   * error. After an abort (a cancel, a suspend) its answer is dropped: the operation
   * neither runs nor is answered here. Settle once `signal` aborts: suspend waits for
   * it, as it does for `run`.
   */
  admit?(operation: Operation, signal: AbortSignal): Promise<Outcome | null>;
  /** The server recorded this operation's outcome (op_ack). */
  acknowledged?(id: string): void;
  /**
   * End what goes on locally that no operation holds, such as background
   * processes, once the computer's access has ended. The executor still runs
   * operations afterwards.
   */
  end?(): Promise<void>;
}

export class OperationRunner {
  private readonly running = new Map<string, AbortController>();
  private readonly inflight = new Set<Promise<void>>();
  private readonly transfers: TransferSender;

  constructor(
    private readonly journal: OperationJournal,
    private readonly executor: Executor,
    private readonly send: Send,
    // What failed outside the executor. A start or an answer that throws comes
    // before anything ran and leaves the row "received", to be asked again. A send
    // that throws leaves the row finished, and it is sent again at the next welcome;
    // a finish that throws leaves it "started", so the next launch answers it
    // "interrupted".
    private readonly onError?: (error: unknown) => void,
  ) {
    this.transfers = new TransferSender(send, (id, seq) => journal.chunk(id, seq), onError);
  }

  /** The link is back: send every outcome the server has not acknowledged, each transfer from its start. */
  connected(): void {
    this.transfers.restart();
    for (const result of this.journal.unsent()) this.deliver(result, result.outcome);
  }

  operation(operation: Operation): void {
    const received = this.journal.receive(operation);
    if (received.action === "reply") {
      this.deliver(operation, received.outcome);
      return;
    }
    if (received.action === "ignore" || this.running.has(operation.id)) return;
    const controller = new AbortController();
    this.running.set(operation.id, controller);
    const done: Promise<void> = this.admitted(operation, controller.signal)
      .catch((error: unknown) => report(this.onError, error))
      .finally(() => {
        if (this.running.get(operation.id) === controller) this.running.delete(operation.id);
        this.inflight.delete(done);
      });
    this.inflight.add(done);
  }

  cancel(id: string): void {
    this.journal.cancel(id);
    this.running.get(id)?.abort();
  }

  /**
   * Stop every running operation and record it as *outcome*; settles once each is
   * recorded. One still in admit is dropped: it stays "received", and is asked again
   * at the next launch.
   */
  suspend(outcome: Outcome): Promise<void> {
    for (const controller of this.running.values()) controller.abort(new Suspension(outcome));
    return Promise.all(this.inflight).then(() => {});
  }

  acknowledged(id: string): void {
    this.journal.acknowledge(id);
    this.transfers.done(id);
    this.executor.acknowledged?.(id);
  }

  /** The server closed this operation and does not want its transfer: it is never sent again. */
  unwanted(id: string): void {
    this.journal.acknowledge(id);
    this.transfers.done(id);
  }

  chunkAcked(id: string, seq: number): void {
    this.transfers.acked(id, seq);
  }

  openIds(): string[] {
    return this.journal.openIds();
  }

  // What the executor answers before the operation starts is recorded without a
  // start. A cancel meanwhile has marked it cancelled; one suspended stays received,
  // and is asked again at the next launch.
  private async admitted(operation: Operation, signal: AbortSignal): Promise<void> {
    let answer: Outcome | null;
    try {
      answer = (await this.executor.admit?.(operation, signal)) ?? null;
    } catch (error) {
      answer = { error: { type: "other", message: error instanceof Error ? error.message : String(error) } };
    }
    if (signal.aborted) return;
    if (answer) {
      const outcome = sendable(operation, answer, false);
      if (this.journal.answer(operation.id, outcome)) this.deliver(operation, outcome);
      return;
    }
    // The journal's claim decides: false if it was cancelled or started meanwhile.
    if (!this.journal.start(operation.id)) return;
    await this.execute(operation, signal);
  }

  private async execute(operation: Operation, signal: AbortSignal): Promise<void> {
    let outcome: Outcome;
    let chunks: Buffer[] = [];
    try {
      ({ outcome, chunks } = carried(operation, await this.executor.run(operation, signal)));
      outcome = sendable(operation, outcome, true);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      outcome = sendable(operation, { error: { type: "other", message } }, true);
    } finally {
      this.running.delete(operation.id);
    }
    // Stopped by suspend: what it did is unknown, whatever the executor said.
    if (signal.reason instanceof Suspension) {
      outcome = signal.reason.outcome;
      chunks = [];
    }
    // A cancelled operation's late outcome is dropped: the server gave up on it.
    let finished: boolean;
    try {
      finished = this.journal.finish(operation.id, outcome, chunks);
    } catch (error) {
      if (chunks.length === 0) throw error;
      // The read is answered with why; only a failure of this reaches onError.
      outcome = isRecord(error) && error.errcode === SQLITE_FULL ? NO_SPACE : NOT_KEPT;
      finished = this.journal.finish(operation.id, outcome);
    }
    if (finished) this.deliver(operation, outcome);
  }

  // A result that names a transfer waits its turn; any other goes at once.
  private deliver(operation: { id: string; digest: string }, outcome: Outcome): void {
    if (transferOf(outcome) !== null) this.transfers.add(operation.id, operation.digest, outcome);
    else this.send(opResult(operation, outcome));
  }
}

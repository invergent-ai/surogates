// Results too large for one frame: a header naming the transfer, then its data in
// numbered chunks, read from the journal as they go (surogates/devices/link.py).
// One transfer at a time, in the order they were given. At most TRANSFER_WINDOW
// chunks the server has not acknowledged, and a chunk only once ws has written the
// one before it to the socket: ws's bufferedAmount never holds more than one
// chunk, so a ping waits behind at most that.

import { CHUNK_BYTES, chunkFrame, opResult, type Outcome, TRANSFER_WINDOW, transferOf } from "../link/protocol.js";
import { report } from "../report.js";

export type Send = (frame: Record<string, unknown>, written?: () => void) => boolean;

interface Sending {
  id: string;
  digest: string;
  outcome: Outcome;
  count: number; // its chunks
  next: number; // the next chunk to send
  acked: number; // how many the server has acknowledged
  writing: boolean; // a chunk ws has not written to the socket yet
}

export class TransferSender {
  private readonly queue: Sending[] = [];
  private current: Sending | null = null;
  // Counts connections: what an earlier one's socket calls back changes nothing.
  private connection = 0;

  constructor(
    private readonly send: Send,
    private readonly chunk: (id: string, seq: number) => Buffer | null,
    private readonly onError?: (error: unknown) => void,
  ) {}

  /** Send the transfer this result names, after those before it. One already queued or on its way is not added again. */
  add(id: string, digest: string, outcome: Outcome): void {
    const transfer = transferOf(outcome);
    if (transfer === null || this.current?.id === id || this.queue.some((s) => s.id === id)) return;
    const count = Math.ceil(transfer.size / CHUNK_BYTES);
    this.queue.push({ id, digest, outcome, count, next: 0, acked: 0, writing: false });
    this.pump();
  }

  /** A new connection: what was on its way is lost, and every transfer is added again from its header. */
  restart(): void {
    this.connection += 1;
    this.current = null;
    this.queue.length = 0;
  }

  acked(id: string, seq: number): void {
    if (this.current?.id !== id) return;
    this.current.acked = Math.max(this.current.acked, seq + 1);
    this.pump();
  }

  /** The server recorded this result, or does not want it: the next transfer goes. */
  done(id: string): void {
    const queued = this.queue.findIndex((s) => s.id === id);
    if (queued >= 0) this.queue.splice(queued, 1);
    if (this.current?.id !== id) return;
    this.current = null;
    this.pump();
  }

  private pump(): void {
    if (this.current === null) {
      const next = this.queue.shift();
      if (next === undefined) return;
      // Acknowledged already: its chunks are gone, and the server has its result.
      if (this.chunk(next.id, 0) === null) {
        this.pump();
        return;
      }
      // Offline: the next connection adds it again.
      if (!this.send(opResult(next, next.outcome))) return;
      this.current = next;
    }
    const sending = this.current;
    if (sending.writing || sending.next >= sending.count || sending.next - sending.acked >= TRANSFER_WINDOW) return;
    const data = this.chunk(sending.id, sending.next);
    if (data === null) return;
    const connection = this.connection;
    const sent = this.send(chunkFrame(sending.id, sending.next, data), () => {
      if (connection !== this.connection || this.current !== sending) return;
      sending.writing = false;
      // Called by ws, where a throw would be uncaught.
      try {
        this.pump();
      } catch (error) {
        report(this.onError, error);
      }
    });
    if (!sent) return;
    sending.writing = true;
    sending.next += 1;
  }
}

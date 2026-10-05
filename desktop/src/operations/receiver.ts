// Writes' data too large for one frame: after its op, the server sends it in numbered
// chunks (surogates/devices/link.py). Each chunk is put in its place as it comes, and the
// data counts only once it is whole and hashes to the SHA-256 its op names. It is held in
// memory, never in the session's temp folder, which the chat's commands can write: an app
// that stops loses it, and the server sends it again with its op. On a new connection
// the server sends each one again from chunk 0.

import { createHash, type Hash } from "node:crypto";

import { CHUNK_BYTES, type Transfer } from "../link/protocol.js";

interface Receiving {
  transfer: Transfer;
  data: Buffer | null; // made at its first chunk, not at its op: the server sends one transfer at a time
  received: number;
  next: number;
  hash: Hash;
  done: (data: Buffer | null) => void;
}

export class TransferReceiver {
  private readonly receiving = new Map<string, Receiving>();

  /**
   * The data of write *id*, once it is whole and matches *transfer*. Null when it does not:
   * a chunk out of order, of the wrong size, or a SHA-256 that differs; and null once
   * *signal* aborts.
   */
  whole(id: string, transfer: Transfer, signal: AbortSignal): Promise<Buffer | null> {
    return new Promise((resolve) => {
      const done = (data: Buffer | null) => {
        if (this.receiving.get(id)?.done === done) this.receiving.delete(id);
        signal.removeEventListener("abort", aborted);
        resolve(data);
      };
      const aborted = () => done(null);
      signal.addEventListener("abort", aborted, { once: true });
      this.receiving.set(id, { transfer, data: null, received: 0, next: 0, hash: createHash("sha256"), done });
    });
  }

  /** One chunk the server sent. One for no write waiting for its data (stopped, or whole already) is dropped. */
  chunk(id: string, seq: number, data: Buffer): void {
    const receiving = this.receiving.get(id);
    if (receiving === undefined) return;
    const { transfer } = receiving;
    if (seq !== receiving.next || data.length !== Math.min(CHUNK_BYTES, transfer.size - receiving.received)) {
      receiving.done(null);
      return;
    }
    receiving.data ??= Buffer.allocUnsafe(transfer.size);
    data.copy(receiving.data, receiving.received);
    receiving.hash.update(data);
    receiving.received += data.length;
    receiving.next += 1;
    if (receiving.received < transfer.size) return;
    receiving.done(receiving.hash.digest("hex") === transfer.sha256 ? receiving.data : null);
  }

  /** A new connection: what came of each write's data on the last one is dropped. */
  restart(): void {
    for (const receiving of this.receiving.values()) {
      receiving.received = 0;
      receiving.next = 0;
      receiving.hash = createHash("sha256");
    }
  }
}

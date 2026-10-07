// What this computer did with each operation it was sent, kept on disk so a
// reconnect or a crash never runs one twice. One row per operation id:
//
//   received      sent to us, not started; safe to run (nothing happened yet). One the
//                 executor answers before it starts (admit) goes straight to finished
//   started       written durably before the operation acts
//   finished      its outcome, written durably before it is sent; a read's data too
//                 large for one frame is kept beside it as the transfer's chunks
//   acknowledged  the server recorded the outcome; its chunks go at once, and after
//                 RETAIN_MS its payload
//   cancelled     never to run; a cancel may arrive before its op, so an unknown
//                 id gets a row too
//
// An operation still "started" when the app starts again was cut off midway:
// it is answered "interrupted" and never run again.

import { DatabaseSync } from "node:sqlite";

import type { Operation, Outcome } from "../link/protocol.js";
import { Bindings } from "./bindings.js";

export const INTERRUPTED: Outcome = {
  error: {
    type: "interrupted",
    message:
      "interrupted: the app stopped while this ran, so it was not run again. Check before repeating it.",
  },
};

// The most unfinished operations a hello reports.
export const MAX_OPEN_REPORTED = 1000;

// How long an acknowledged outcome is kept before its payload is dropped.
export const RETAIN_MS = 24 * 60 * 60 * 1000;

export type Received =
  | { action: "run" }
  | { action: "reply"; outcome: Outcome }
  | { action: "ignore" };

interface Row {
  digest: string | null;
  state: string;
  outcome: string | null;
}

export class OperationJournal {
  private readonly db: DatabaseSync;

  constructor(path: string, private readonly now: () => number = Date.now) {
    this.db = new DatabaseSync(path);
    try {
      // One journal per file. Another journal opening it would run recovery
      // and turn this one's running operations into "interrupted".
      this.db.exec(`
        PRAGMA locking_mode = EXCLUSIVE;
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = FULL;
        PRAGMA fullfsync = ON;
        PRAGMA checkpoint_fullfsync = ON;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS operations (
          id TEXT PRIMARY KEY,
          digest TEXT,
          state TEXT NOT NULL,
          outcome TEXT,
          updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS operations_by_state ON operations (state, updated_at);
        CREATE TABLE IF NOT EXISTS bindings (
          root TEXT PRIMARY KEY,
          nonce TEXT NOT NULL,
          folder TEXT NOT NULL,
          dev INTEGER NOT NULL,
          ino INTEGER NOT NULL,
          boot TEXT NOT NULL,
          mode TEXT NOT NULL,
          bound_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS domains (
          root TEXT NOT NULL,
          domain TEXT NOT NULL,
          PRIMARY KEY (root, domain)
        );
        CREATE TABLE IF NOT EXISTS chunks (
          id TEXT NOT NULL,
          seq INTEGER NOT NULL,
          data BLOB NOT NULL,
          PRIMARY KEY (id, seq)
        );
      `);
      this.bindings = new Bindings(this.db);
      // Opened once per process: what a crash cut off is interrupted before
      // anything is reported or run.
      this.recovered = this.recover();
    } catch (error) {
      // The file is held by another journal: fail loudly, leave no handle open.
      this.db.close();
      throw error;
    }
  }

  /** How many operations a crash had cut off, answered "interrupted" when this journal was opened. */
  readonly recovered: number;

  /** The folder each root session works on: in this file, under its lock. */
  readonly bindings: Bindings;

  /** Tie this journal to one device; false if it already belongs to another. */
  claim(deviceId: string): boolean {
    this.db.prepare(`INSERT INTO meta (key, value) VALUES ('device_id', ?) ON CONFLICT (key) DO NOTHING`).run(deviceId);
    const row = this.db.prepare(`SELECT value FROM meta WHERE key = 'device_id'`).get() as { value: string };
    return row.value === deviceId;
  }

  private recover(): number {
    const result = this.db
      .prepare(`UPDATE operations SET state = 'finished', outcome = ?, updated_at = ? WHERE state = 'started'`)
      .run(JSON.stringify(INTERRUPTED), this.now());
    return Number(result.changes);
  }

  /** What to do with an operation the server sent. */
  receive(operation: Operation): Received {
    const row = this.db
      .prepare(`SELECT digest, state, outcome FROM operations WHERE id = ?`)
      .get(operation.id) as Row | undefined;
    if (row === undefined) {
      this.db
        .prepare(`INSERT INTO operations (id, digest, state, updated_at) VALUES (?, ?, 'received', ?)`)
        .run(operation.id, operation.digest, this.now());
      return { action: "run" };
    }
    // A cancelled one never runs, nor does another request under a known id.
    if (row.state === "cancelled" || row.digest !== operation.digest) {
      return { action: "ignore" };
    }
    if (row.state === "finished" || row.state === "acknowledged") {
      return row.outcome === null
        ? { action: "ignore" }
        : { action: "reply", outcome: JSON.parse(row.outcome) as Outcome };
    }
    // "started" is running here: a restart would have made it interrupted.
    return row.state === "received" ? { action: "run" } : { action: "ignore" };
  }

  /**
   * Durably mark an operation started, before it acts. True only when this call
   * moved it from received to started; false (do not act) when it was cancelled,
   * unknown, or already started or finished.
   */
  start(id: string): boolean {
    const result = this.db
      .prepare(`UPDATE operations SET state = 'started', updated_at = ? WHERE id = ? AND state = 'received'`)
      .run(this.now(), id);
    return Number(result.changes) === 1;
  }

  /**
   * Durably record an outcome, with the chunks of the transfer it names, before it
   * is sent. True when this call recorded it. False, with nothing recorded, when the
   * operation was cancelled meanwhile, was already finished, is unknown, or never started.
   */
  finish(id: string, outcome: Outcome, chunks: readonly Buffer[] = []): boolean {
    // JSON.stringify drops an undefined ok, leaving a frame the server refuses.
    const stored = "ok" in outcome && outcome.ok === undefined ? { ok: null } : outcome;
    return this.transaction(() => {
      const result = this.db
        .prepare(`UPDATE operations SET state = 'finished', outcome = ?, updated_at = ? WHERE id = ? AND state = 'started'`)
        .run(JSON.stringify(stored), this.now(), id);
      if (Number(result.changes) !== 1) return false;
      const insert = this.db.prepare(`INSERT INTO chunks (id, seq, data) VALUES (?, ?, ?)`);
      chunks.forEach((data, seq) => insert.run(id, seq, data));
      return true;
    });
  }

  /** Chunk *seq* of the transfer a finished outcome names, or null once it is gone. */
  chunk(id: string, seq: number): Buffer | null {
    const row = this.db.prepare(`SELECT data FROM chunks WHERE id = ? AND seq = ?`).get(id, seq) as
      | { data: Uint8Array }
      | undefined;
    return row === undefined ? null : Buffer.from(row.data.buffer, row.data.byteOffset, row.data.byteLength);
  }

  /**
   * Durably record the outcome of an operation that never started, before it is
   * sent. True when this call recorded it; false, with nothing recorded, when it
   * was cancelled meanwhile, or is unknown, started or finished.
   */
  answer(id: string, outcome: Outcome): boolean {
    const stored = "ok" in outcome && outcome.ok === undefined ? { ok: null } : outcome;
    const result = this.db
      .prepare(`UPDATE operations SET state = 'finished', outcome = ?, updated_at = ? WHERE id = ? AND state = 'received'`)
      .run(JSON.stringify(stored), this.now(), id);
    return Number(result.changes) === 1;
  }

  /** The server recorded the outcome: its transfer's chunks are not needed again. */
  acknowledge(id: string): void {
    this.transaction(() => {
      this.db
        .prepare(`UPDATE operations SET state = 'acknowledged', updated_at = ? WHERE id = ? AND state = 'finished'`)
        .run(this.now(), id);
      this.db.prepare(`DELETE FROM chunks WHERE id = ?`).run(id);
    });
  }

  /** Never run this operation; what already finished keeps its outcome. */
  cancel(id: string): void {
    const now = this.now();
    this.db
      .prepare(`
        INSERT INTO operations (id, digest, state, updated_at) VALUES (?, NULL, 'cancelled', ?)
        ON CONFLICT (id) DO UPDATE SET state = 'cancelled', updated_at = excluded.updated_at
          WHERE operations.state IN ('received', 'started')
      `)
      .run(id, now);
  }

  /** What a hello reports as held unfinished. */
  openIds(): string[] {
    const rows = this.db
      .prepare(`SELECT id FROM operations WHERE state IN ('received', 'started') ORDER BY updated_at DESC, rowid DESC LIMIT ?`)
      .all(MAX_OPEN_REPORTED) as Array<{ id: string }>;
    return rows.map((row) => row.id);
  }

  /** Outcomes the server has not acknowledged, to send again after a reconnect. */
  unsent(): Array<{ id: string; digest: string; outcome: Outcome }> {
    const rows = this.db
      .prepare(`SELECT id, digest, outcome FROM operations WHERE state = 'finished' ORDER BY updated_at, id`)
      .all() as Array<{ id: string; digest: string; outcome: string }>;
    return rows.map((row) => ({ id: row.id, digest: row.digest, outcome: JSON.parse(row.outcome) as Outcome }));
  }

  /**
   * The agent ended this device's token: it closed every operation the device held, so none is
   * sent or run again. The transfers' chunks go, every operation not yet acknowledged is cancelled
   * and its outcome dropped, and the bindings stay, for a restore.
   */
  retire(): void {
    this.transaction(() => {
      this.db.exec("DELETE FROM chunks");
      this.db
        .prepare(`UPDATE operations SET state = 'cancelled', outcome = NULL, updated_at = ? WHERE state IN ('received', 'started', 'finished')`)
        .run(this.now());
    });
  }

  /** Drop the payload of outcomes acknowledged more than RETAIN_MS ago; the record stays. */
  prune(): number {
    const result = this.db
      .prepare(`UPDATE operations SET outcome = NULL WHERE state = 'acknowledged' AND outcome IS NOT NULL AND updated_at < ?`)
      .run(this.now() - RETAIN_MS);
    return Number(result.changes);
  }

  /**
   * Give back the disk the -wal took, which a failed write leaves at its size until
   * close. Best effort: what fails here is retried at the next checkpoint.
   */
  checkpoint(): void {
    try {
      this.db.exec("PRAGMA wal_checkpoint(TRUNCATE)");
    } catch {
      // The -wal keeps its size until the next checkpoint or close.
    }
  }

  close(): void {
    this.db.close();
  }

  private transaction<T>(work: () => T): T {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const result = work();
      this.db.exec("COMMIT");
      return result;
    } catch (error) {
      // SQLite rolls back by itself on some errors, a full disk among them: the
      // error to throw is still the one that failed the work.
      try {
        this.db.exec("ROLLBACK");
      } catch {
        // No transaction was left to roll back.
      }
      throw error;
    }
  }
}

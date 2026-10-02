// What this computer did with each operation it was sent, kept on disk so a
// reconnect or a crash never runs one twice. One row per operation id:
//
//   received      sent to us, not started; safe to run (nothing happened yet)
//   started       written durably before the operation acts
//   finished      its outcome, written durably before it is sent
//   acknowledged  the server recorded the outcome; after RETAIN_MS its payload goes
//   cancelled     never to run; a cancel may arrive before its op, so an unknown
//                 id gets a row too
//
// An operation still "started" when the app starts again was cut off midway:
// it is answered "interrupted" and never run again.

import { DatabaseSync } from "node:sqlite";

import type { Operation, Outcome } from "../link/protocol.js";

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
    this.db.exec(`
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
    `);
    // Opened once per process: what a crash cut off is interrupted before
    // anything is reported or run.
    this.recovered = this.recover();
  }

  /** How many operations a crash had cut off, answered "interrupted" when this journal was opened. */
  readonly recovered: number;

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

  /** Durably mark an operation started, before it acts. */
  start(id: string): void {
    this.db
      .prepare(`UPDATE operations SET state = 'started', updated_at = ? WHERE id = ? AND state = 'received'`)
      .run(this.now(), id);
  }

  /** Durably record an outcome, before it is sent. False if it was cancelled meanwhile. */
  finish(id: string, outcome: Outcome): boolean {
    const result = this.db
      .prepare(`UPDATE operations SET state = 'finished', outcome = ?, updated_at = ? WHERE id = ? AND state = 'started'`)
      .run(JSON.stringify(outcome), this.now(), id);
    return Number(result.changes) === 1;
  }

  /** The server recorded the outcome. */
  acknowledge(id: string): void {
    this.db
      .prepare(`UPDATE operations SET state = 'acknowledged', updated_at = ? WHERE id = ? AND state = 'finished'`)
      .run(this.now(), id);
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
      .prepare(`SELECT id FROM operations WHERE state IN ('received', 'started') ORDER BY updated_at DESC, id LIMIT ?`)
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

  /** Drop the payload of outcomes acknowledged more than RETAIN_MS ago; the record stays. */
  prune(): number {
    const result = this.db
      .prepare(`UPDATE operations SET outcome = NULL WHERE state = 'acknowledged' AND outcome IS NOT NULL AND updated_at < ?`)
      .run(this.now() - RETAIN_MS);
    return Number(result.changes);
  }

  close(): void {
    this.db.close();
  }
}

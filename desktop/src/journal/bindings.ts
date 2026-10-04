// The folder each root session works on, as this computer's user confirmed it
// (spec, Sections 2 and 8). Kept in the device's journal file, through the
// journal's own handle: the file is opened with an exclusive lock, and one device
// is one agent identity, which is what a binding belongs to.

import type { DatabaseSync } from "node:sqlite";

// Work freely (the default), or ask every time.
export type Mode = "free" | "ask";

export interface Binding {
  root: string; // the root session; every operation for it and its children names it
  nonce: string; // the confirmation it was bound under
  folder: string; // resolved, and exactly what the server was told
  dev: number; // the folder's identity when it was confirmed: one replaced since is not this chat's
  ino: number;
  mode: Mode;
  boundAt: number;
}

interface Row {
  root: string;
  nonce: string;
  folder: string;
  dev: number;
  ino: number;
  mode: string;
  bound_at: number;
}

const read = (row: Row | undefined): Binding | undefined =>
  row && {
    root: row.root, nonce: row.nonce, folder: row.folder, dev: row.dev, ino: row.ino,
    mode: row.mode as Mode, boundAt: row.bound_at,
  };

export class Bindings {
  constructor(private readonly db: DatabaseSync) {}

  /** Record a root's binding. A root is bound once: a second binding for it throws. */
  add(binding: Binding): void {
    this.db
      .prepare(`INSERT INTO bindings (root, nonce, folder, dev, ino, mode, bound_at) VALUES (?, ?, ?, ?, ?, ?, ?)`)
      .run(binding.root, binding.nonce, binding.folder, binding.dev, binding.ino, binding.mode, binding.boundAt);
  }

  get(root: string): Binding | undefined {
    return read(this.db.prepare(`SELECT * FROM bindings WHERE root = ?`).get(root) as Row | undefined);
  }

  /** The latest binding: a new chat's folder, unless the user picks another. */
  last(): Binding | undefined {
    return read(this.db.prepare(`SELECT * FROM bindings ORDER BY bound_at DESC, rowid DESC LIMIT 1`).get() as Row | undefined);
  }
}

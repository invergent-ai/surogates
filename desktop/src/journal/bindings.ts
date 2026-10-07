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
  boot: string; // the boot dev was read in: a reboot can renumber a mount; "" when unread
  mode: Mode;
  boundAt: number;
}

// Read as bigints: node:sqlite throws on an INTEGER above 2^53, and SMB/CIFS and
// overlayfs give inode numbers that large. Each began as a JS number, so Number()
// gives it back exactly.
interface Row {
  root: string;
  nonce: string;
  folder: string;
  dev: bigint;
  ino: bigint;
  boot: string;
  mode: string;
  bound_at: bigint;
}

const read = (row: Row | undefined): Binding | undefined =>
  row && {
    root: row.root, nonce: row.nonce, folder: row.folder, dev: Number(row.dev), ino: Number(row.ino), boot: row.boot,
    mode: row.mode as Mode, boundAt: Number(row.bound_at),
  };

export class Bindings {
  private readonly listeners = new Set<(root: string) => void>();

  constructor(private readonly db: DatabaseSync) {}

  /** Hear each root bound, and each change of a root's mode, once it is written; the returned function stops it. */
  watch(listener: (root: string) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Record a root's binding. A root is bound once: a second binding for it throws. */
  add(binding: Binding): void {
    this.db
      .prepare(`INSERT INTO bindings (root, nonce, folder, dev, ino, boot, mode, bound_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`)
      .run(binding.root, binding.nonce, binding.folder, binding.dev, binding.ino, binding.boot, binding.mode, binding.boundAt);
    this.changed(binding.root);
  }

  /**
   * Forget a deleted root's binding and what its user allowed for it, both or neither.
   * The folder is not touched; an unknown root changes nothing.
   */
  retire(root: string): void {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      this.db.prepare(`DELETE FROM domains WHERE root = ?`).run(root);
      this.db.prepare(`DELETE FROM bindings WHERE root = ?`).run(root);
      this.db.exec("COMMIT");
    } catch (error) {
      try {
        this.db.exec("ROLLBACK");
      } catch {
        // SQLite rolled back by itself, as on a full disk.
      }
      throw error;
    }
  }

  /** A root's mode from now on, for it and its sub-agents. An unknown root changes nothing. */
  setMode(root: string, mode: Mode): void {
    // Told only when it changed: an unknown root, or the mode it has, changes nothing.
    const { changes } = this.db.prepare(`UPDATE bindings SET mode = ? WHERE root = ? AND mode <> ?`).run(mode, root, mode);
    if (changes > 0) this.changed(root);
  }

  // A listener's failure is not the write's: it is written, and the others hear it.
  private changed(root: string): void {
    for (const listener of this.listeners) {
      try {
        listener(root);
      } catch {
        // Nothing here can tell anyone more.
      }
    }
  }

  /** Let *domain* (a host, as srt's allowedDomains takes it) through for a bound root from now on. Once each; an unknown root changes nothing. */
  allowDomain(root: string, domain: string): void {
    this.db.prepare(`INSERT OR IGNORE INTO domains (root, domain) SELECT root, ? FROM bindings WHERE root = ?`).run(domain, root);
  }

  /** What a root's user allowed for it past the package hosts, in the order allowed. */
  domains(root: string): string[] {
    const rows = this.db.prepare(`SELECT domain FROM domains WHERE root = ? ORDER BY rowid`).all(root) as Array<{ domain: string }>;
    return rows.map((row) => row.domain);
  }

  get(root: string): Binding | undefined {
    const select = this.db.prepare(`SELECT * FROM bindings WHERE root = ?`);
    select.setReadBigInts(true);
    return read(select.get(root) as Row | undefined);
  }

  /** Every folder a chat is bound to, once each, the first bound first: what a restore names. */
  folders(): string[] {
    const rows = this.db.prepare(`SELECT folder FROM bindings GROUP BY folder ORDER BY MIN(bound_at), MIN(rowid)`).all() as Array<{ folder: string }>;
    return rows.map((row) => row.folder);
  }

  /** The latest binding: a new chat's folder, unless the user picks another. */
  last(): Binding | undefined {
    const select = this.db.prepare(`SELECT * FROM bindings ORDER BY bound_at DESC, rowid DESC LIMIT 1`);
    select.setReadBigInts(true);
    return read(select.get() as Row | undefined);
  }
}

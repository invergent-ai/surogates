// The folder each root session works on, as this computer's user confirmed it
// (spec, Sections 2 and 8). Kept in the device's journal file, through the
// journal's own handle: the file is opened with an exclusive lock, and one device
// is one agent identity, which is what a binding belongs to.

import type { DatabaseSync } from "node:sqlite";

import { SANDBOX_PORTS } from "../browser/ports.js";

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

  /**
   * Hear each root bound, each change of a root's mode, each host allowed for it, each port of its own servers allowed
   * for its browser or taken from it, and each root's binding forgotten, once it is written; the returned function stops it.
   */
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
   * The folder is not touched; an unknown root changes nothing, and tells nothing.
   */
  retire(root: string): void {
    this.db.exec("BEGIN IMMEDIATE");
    let forgotten: number | bigint;
    try {
      this.db.prepare(`DELETE FROM domains WHERE root = ?`).run(root);
      this.db.prepare(`DELETE FROM browsing WHERE root = ?`).run(root);
      this.db.prepare(`DELETE FROM browser_ports WHERE root = ?`).run(root);
      forgotten = this.db.prepare(`DELETE FROM bindings WHERE root = ?`).run(root).changes;
      this.db.exec("COMMIT");
    } catch (error) {
      try {
        this.db.exec("ROLLBACK");
      } catch {
        // SQLite rolled back by itself, as on a full disk.
      }
      throw error;
    }
    if (forgotten > 0) this.changed(root);
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
    // Told only when kept: a host allowed already, or a root with no binding, changes nothing.
    const { changes } = this.db.prepare(`INSERT OR IGNORE INTO domains (root, domain) SELECT root, ? FROM bindings WHERE root = ?`).run(domain, root);
    if (changes > 0) this.changed(root);
  }

  /** Take *domain* back from a root: its next connection there asks again. One never allowed changes nothing. */
  disallowDomain(root: string, domain: string): void {
    this.db.prepare(`DELETE FROM domains WHERE root = ? AND domain = ?`).run(root, domain);
  }

  /** What a root's user allowed for it past the package hosts, in the order allowed. */
  domains(root: string): string[] {
    const rows = this.db.prepare(`SELECT domain FROM domains WHERE root = ? ORDER BY rowid`).all(root) as Array<{ domain: string }>;
    return rows.map((row) => row.domain);
  }

  /** Let a bound root's agent use the browser on this computer from now on, as its user allowed. An unknown root changes nothing. */
  allowBrowser(root: string): void {
    // Told only when kept: a root allowed already, or one with no binding, changes nothing.
    const { changes } = this.db.prepare(`INSERT OR IGNORE INTO browsing (root) SELECT root FROM bindings WHERE root = ?`).run(root);
    if (changes > 0) this.changed(root);
  }

  /**
   * Take the browser back from a root: its agent's next browser call asks its first use again, and the ports of its
   * own servers go with it, first. One not allowed changes nothing.
   */
  disallowBrowser(root: string): void {
    const { changes } = this.db.prepare(`DELETE FROM browser_ports WHERE root = ?`).run(root);
    this.db.prepare(`DELETE FROM browsing WHERE root = ?`).run(root);
    if (changes > 0) this.changed(root);
  }

  /**
   * Let the browser open *port* of a bound root's own servers from now on, as its user allowed. One root has a port
   * at a time, as the browser's one profile cannot tell its chats apart: another root's hold on it goes. An unknown
   * root changes nothing. Throws for a port of the sandbox's own proxies, which is no chat's server.
   */
  allowPort(root: string, port: number): void {
    if (SANDBOX_PORTS.has(port)) throw new Error(`Port ${port} is the sandbox's own proxy`);
    const former = this.portOwner(port);
    if (former === root) return;
    const { changes } = this.db.prepare(`INSERT OR REPLACE INTO browser_ports (port, root) SELECT ?, root FROM bindings WHERE root = ?`).run(port, root);
    if (changes === 0) return;
    if (former !== undefined) this.changed(former);
    this.changed(root);
  }

  /** Take *port* back from a root: the browser reaches it no more. One the root does not have changes nothing. */
  disallowPort(root: string, port: number): void {
    const { changes } = this.db.prepare(`DELETE FROM browser_ports WHERE root = ? AND port = ?`).run(root, port);
    if (changes > 0) this.changed(root);
  }

  /** The ports of a root's own servers the browser may open, lowest first. */
  ports(root: string): number[] {
    const rows = this.db.prepare(`SELECT port FROM browser_ports WHERE root = ? ORDER BY port`).all(root) as Array<{ port: number }>;
    return rows.map((row) => row.port);
  }

  /** The root whose servers have *port* in the browser, if any. */
  portOwner(port: number): string | undefined {
    return (this.db.prepare(`SELECT root FROM browser_ports WHERE port = ?`).get(port) as { root: string } | undefined)?.root;
  }

  /** Every port the browser may open, with the root whose servers it leads to, lowest first: what its proxy and the sandbox are told. */
  forwards(): Array<{ port: number; root: string }> {
    return (this.db.prepare(`SELECT port, root FROM browser_ports ORDER BY port`).all() as Array<{ port: number; root: string }>)
      .map(({ port, root }) => ({ port, root }));
  }

  /** Whether the root's user let its agent use the browser on this computer. */
  browsing(root: string): boolean {
    return this.db.prepare(`SELECT 1 FROM browsing WHERE root = ?`).get(root) !== undefined;
  }

  get(root: string): Binding | undefined {
    const select = this.db.prepare(`SELECT * FROM bindings WHERE root = ?`);
    select.setReadBigInts(true);
    return read(select.get(root) as Row | undefined);
  }

  /** Every binding, the first bound first. */
  all(): Binding[] {
    const select = this.db.prepare(`SELECT * FROM bindings ORDER BY bound_at, rowid`);
    select.setReadBigInts(true);
    return (select.all() as unknown as Row[]).map((row) => read(row)!);
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

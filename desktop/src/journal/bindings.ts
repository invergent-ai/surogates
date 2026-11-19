// The folder each root session works on, as this computer's user confirmed it
// (spec, Sections 2 and 8). Kept in the device's journal file, through the
// journal's own handle: the file is opened with an exclusive lock, and one device
// is one agent identity, which is what a binding belongs to.

import { randomBytes } from "node:crypto";
import type { DatabaseSync } from "node:sqlite";

import { SANDBOX_PORTS } from "../browser/ports.js";
import { inside } from "../files/paths.js";

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
  // A project's thread, which works in a copy of the folder that is its own: the thread, which is
  // the root itself. Without it, a chat that works in the folder itself.
  history?: string;
  // The coordinator of a project made here, on the folder its user chose for the project: each of the
  // project's threads works in a copy of it, made with no sheet. Without it, no project's.
  project?: true;
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
  history: string | null;
  project: bigint | null;
}

const read = (row: Row | undefined): Binding | undefined =>
  row && {
    root: row.root, nonce: row.nonce, folder: row.folder, dev: Number(row.dev), ino: Number(row.ino), boot: row.boot,
    mode: row.mode as Mode, boundAt: Number(row.bound_at), ...(row.history === null ? {} : { history: row.history }),
    ...(row.project ? { project: true } : {}),
  };

export class Bindings {
  private readonly listeners = new Set<(root: string) => void>();
  // Each root's folders granted for its turn alone (grantRead): never journaled, so a restart ends them.
  private readonly turnReads = new Map<string, Set<string>>();

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
    this.insert(binding);
    this.changed(binding.root);
  }

  private insert(binding: Binding): void {
    this.db
      .prepare(`INSERT INTO bindings (root, nonce, folder, dev, ino, boot, mode, bound_at, history, project) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
      .run(
        binding.root, binding.nonce, binding.folder, binding.dev, binding.ino, binding.boot, binding.mode, binding.boundAt,
        binding.history ?? null, binding.project ? 1 : null,
      );
  }

  /**
   * Record a root's binding in place of the one it has, as a project's coordinator is bound afresh to the folder its
   * user chose again for the project: what its user allowed for it stays. A root with none is bound as add binds it.
   */
  replace(binding: Binding): void {
    this.atomically(() => {
      this.db.prepare(`DELETE FROM bindings WHERE root = ?`).run(binding.root);
      this.insert(binding);
    });
    this.changed(binding.root);
  }

  /**
   * Forget a deleted root's binding and what its user allowed for it, both or neither.
   * The folder is not touched; an unknown root changes nothing, and tells nothing.
   */
  retire(root: string): void {
    const forgotten = this.atomically(() => {
      this.db.prepare(`DELETE FROM domains WHERE root = ?`).run(root);
      this.db.prepare(`DELETE FROM read_grants WHERE root = ?`).run(root);
      this.turnReads.delete(root);
      this.db.prepare(`DELETE FROM outside_copies WHERE root = ?`).run(root);
      this.db.prepare(`DELETE FROM browsing WHERE root = ?`).run(root);
      this.takePorts(root);
      return this.db.prepare(`DELETE FROM bindings WHERE root = ?`).run(root).changes;
    });
    if (forgotten > 0) this.changed(root);
  }

  // *write*, kept whole or not at all.
  private atomically<T>(write: () => T): T {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const done = write();
      this.db.exec("COMMIT");
      return done;
    } catch (error) {
      try {
        this.db.exec("ROLLBACK");
      } catch {
        // SQLite rolled back by itself, as on a full disk.
      }
      throw error;
    }
  }

  // *port* has turned: given to a chat, moved to another, or taken back. Its mark is new, one no turn had
  // before, in this journal or in another of the same browser profiles: what was stored at the port's origins
  // before is cleared there (BrowserHost.forwards).
  private turn(port: number): void {
    this.db.prepare(`INSERT INTO browser_port_turns (port, turn) VALUES (?, ?) ON CONFLICT (port) DO UPDATE SET turn = excluded.turn`)
      .run(port, randomBytes(16).toString("hex"));
  }

  // Take every port of *root*'s own servers back, each a turn: how many there were.
  private takePorts(root: string): number {
    const taken = this.db.prepare(`DELETE FROM browser_ports WHERE root = ? RETURNING port`).all(root) as Array<{ port: number }>;
    for (const { port } of taken) this.turn(port);
    return taken.length;
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

  /**
   * Let a bound root's agent read *folder* (a guest path outside its own folder): for the rest of the chat, kept in the
   * journal; or, *forTurn*, only until endTurnReads, kept in memory alone, so a restart ends it too. A folder a grant
   * holds already, for the chat, or for the turn when this one is for the turn too, changes nothing; a grant takes the
   * place of those it holds that last no longer than it, so each folder is listed once. An unknown root changes nothing.
   */
  grantRead(root: string, folder: string, forTurn = false): void {
    if (!this.get(root)) return;
    const turn = this.turnReads.get(root) ?? new Set<string>();
    const kept = this.chatReads(root);
    if (kept.some((grant) => inside(folder, grant)) || (forTurn && [...turn].some((grant) => inside(folder, grant)))) return;
    for (const grant of turn) if (inside(grant, folder)) turn.delete(grant);
    if (forTurn) {
      turn.add(folder);
      this.turnReads.set(root, turn);
    } else {
      this.atomically(() => {
        for (const grant of kept) if (inside(grant, folder)) this.db.prepare(`DELETE FROM read_grants WHERE root = ? AND folder = ?`).run(root, grant);
        this.db.prepare(`INSERT OR IGNORE INTO read_grants (root, folder) VALUES (?, ?)`).run(root, folder);
      });
    }
    this.changed(root);
  }

  // A root's grants for the rest of its chat, in the order granted.
  private chatReads(root: string): string[] {
    const rows = this.db.prepare(`SELECT folder FROM read_grants WHERE root = ? ORDER BY rowid`).all(root) as Array<{ folder: string }>;
    return rows.map((row) => row.folder);
  }

  /** Take *folder* back from a root, for the turn or the chat. One never granted changes nothing, and tells nothing. */
  takeBackRead(root: string, folder: string): void {
    const held = this.turnReads.get(root)?.delete(folder) === true;
    const { changes } = this.db.prepare(`DELETE FROM read_grants WHERE root = ? AND folder = ?`).run(root, folder);
    if (changes > 0 || held) this.changed(root);
  }

  /** End a root's grants for its turn (grantRead's *forTurn*): its turn is over. */
  endTurnReads(root: string): void {
    const held = (this.turnReads.get(root)?.size ?? 0) > 0;
    this.turnReads.delete(root);
    if (held) this.changed(root);
  }

  /** Whether *folder* is a root's grant for its turn alone. */
  forTurn(root: string, folder: string): boolean {
    return this.turnReads.get(root)?.has(folder) === true;
  }

  /** A root's grants for its turn alone, in the order granted. */
  turnFolders(root: string): string[] {
    return [...(this.turnReads.get(root) ?? [])];
  }

  /** The folders a root's user let its agent read outside its own, in the order granted: the chat's, then the turn's. */
  reads(root: string): string[] {
    return [...this.chatReads(root), ...this.turnFolders(root)];
  }

  /** Note that *copy*, in a bound root's folder, was copied from *original* as it was at *revision*; a copy noted before is replaced. */
  recordCopy(root: string, copy: string, original: string, revision: string): void {
    this.db.prepare(`INSERT OR REPLACE INTO outside_copies (root, copy, original, revision) SELECT root, ?, ?, ? FROM bindings WHERE root = ?`)
      .run(copy, original, revision, root);
  }

  /** Where *copy* came from and the revision it was taken at; undefined for a file that is no recorded copy. */
  copyOf(root: string, copy: string): { original: string; revision: string } | undefined {
    return this.db.prepare(`SELECT original, revision FROM outside_copies WHERE root = ? AND copy = ?`).get(root, copy) as
      { original: string; revision: string } | undefined;
  }

  /** The copies a root's folder holds of *original*, in the order made. */
  copiesFrom(root: string, original: string): string[] {
    const rows = this.db.prepare(`SELECT copy FROM outside_copies WHERE root = ? AND original = ? ORDER BY rowid`).all(root, original) as Array<{ copy: string }>;
    return rows.map((row) => row.copy);
  }

  /** Forget that *copy* came from anywhere. */
  forgetCopy(root: string, copy: string): void {
    this.db.prepare(`DELETE FROM outside_copies WHERE root = ? AND copy = ?`).run(root, copy);
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
    const taken = this.atomically(() => {
      const ports = this.takePorts(root);
      this.db.prepare(`DELETE FROM browsing WHERE root = ?`).run(root);
      return ports;
    });
    if (taken > 0) this.changed(root);
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
    const changes = this.atomically(() => {
      const { changes: kept } = this.db.prepare(`INSERT OR REPLACE INTO browser_ports (port, root) SELECT ?, root FROM bindings WHERE root = ?`).run(port, root);
      if (kept > 0) this.turn(port);
      return kept;
    });
    if (changes === 0) return;
    if (former !== undefined) this.changed(former);
    this.changed(root);
  }

  /** Take *port* back from a root: the browser reaches it no more. One the root does not have changes nothing. */
  disallowPort(root: string, port: number): void {
    const changes = this.atomically(() => {
      const { changes: taken } = this.db.prepare(`DELETE FROM browser_ports WHERE root = ? AND port = ?`).run(root, port);
      if (taken > 0) this.turn(port);
      return taken;
    });
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

  /**
   * Every port that has turned, with the mark of its last turn, lowest first: a port given to a chat, moved to another, or
   * taken back, whether or not a chat has it now. What the browser is told, to clear each origin of a port that turned since.
   */
  turns(): Array<{ port: number; turn: string }> {
    return (this.db.prepare(`SELECT port, turn FROM browser_port_turns ORDER BY port`).all() as Array<{ port: number; turn: string }>)
      .map(({ port, turn }) => ({ port, turn }));
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

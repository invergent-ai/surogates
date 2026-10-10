// The binding handshake (spec, Section 8). The page asks for a folder, and the
// user confirms one in the desktop's own sheet; the page creates the chat with
// the confirmation's nonce; the server sends that chat's bind operation, which is
// answered as it arrives, against the confirmation; bindSession tells the page
// once the server has recorded the answer. Every other operation is put to the
// approvals first (approvals.ts), then goes to the tool hosts, which find their
// folder in the bindings.

import { randomBytes } from "node:crypto";
import { mkdirSync, rmdirSync } from "node:fs";
import { lstat } from "node:fs/promises";
import { join } from "node:path";

import { NOT_BOUND } from "../hosts/tool-hosts.js";
import type { Binding, Bindings, Mode } from "../journal/bindings.js";
import type { Operation, Outcome } from "../link/protocol.js";
import type { Executor } from "../operations/runner.js";
import { report } from "../report.js";
import { THREAD } from "../vm/history.js";
import { type ApprovalPrompts, Approvals, type DownloadBy } from "./approvals.js";
import { BOOT_ID, checkFolder, confirmedFolder, type FolderGuards } from "./folder.js";
import { type LinkSummary, scanLinks } from "./links.js";

// How long a confirmed folder waits for its chat's bind operation.
export const PREPARED_MS = 5 * 60_000;

// How long Show folder waits for a look at the folder: a dead network or FUSE mount never answers.
export const LOOK_MS = 5_000;

// What a look at a folder answers: whether it is one, and its identity.
export type FolderLook = { isDirectory(): boolean; dev: number; ino: number };

// The chats whose folder Show folder looks at now, until the look returns, its deadline past or
// not. Kept for the process, not the binder: a stack made again finds the looks still running.
const looking = new Set<string>();
// Looks at once, across chats: one dead mount can hold two of libuv's four threads, never more.
const LOOKS_AT_ONCE = 2;

// The server appends ". Start a new chat." to a binding refusal's message: none ends in ".".
export const ALREADY_BOUND: Outcome = {
  error: { type: "binding", message: "This chat already works on another folder of this computer" },
};
// The binding could not be written to the journal (a full disk, say).
export const NOT_RECORDED: Outcome = {
  error: { type: "binding", message: "This computer could not record the folder for this chat" },
};
// A deleted chat's binding could not be forgotten: the server keeps its answer.
export const NOT_FORGOTTEN: Outcome = {
  error: { type: "binding", message: "This computer could not forget the folder of a deleted chat" },
};
// A project's thread whose tools here make no copy of its folder to work in: it works nowhere, and never in the folder.
const NO_COPY_HERE = "This computer keeps no copy of a folder for a project's thread to work in, so the thread cannot work here";
export const KEEPS_NO_COPY: Outcome = { error: { type: "binding", message: NO_COPY_HERE } };
const BOUND: Outcome = { ok: null };
// A bind that recorded *thread*'s copy says so: the server counts a thread bound on no other answer (surogates/devices/binding.py).
const boundTo = (thread: string | undefined): Outcome => (thread === undefined ? BOUND : { ok: { history: { thread } } });

// The project's thread a folder is asked for: its names, as the page sent them.
export interface ThreadLabel {
  project: string;
  thread: string;
}

// What the sheet shows. Accepting binds *folder* with the mode chosen there.
export interface FolderSheet {
  agent: string; // the agent the chat is with
  folder: string; // resolved
  mode: Mode; // as the sheet opens
  links: LinkSummary | null; // null: no file in the folder is linked from elsewhere
  refusal: string | null; // why no chat may work on this folder: the sheet offers Change and Cancel only
  thread: ThreadLabel | null; // a project's thread the folder is for; null for a new chat
}

// The desktop shell's own windows; fakes in tests. Each call is dismissed by its signal.
export interface FolderPrompts {
  // The new-chat sheet: Enter accepts, Change opens the folder dialog, null is cancel.
  confirmFolder(sheet: FolderSheet, signal: AbortSignal): Promise<{ mode: Mode } | "change" | null>;
  // The operating system's folder dialog; null is cancel.
  pickFolder(startIn: string, signal: AbortSignal): Promise<string | null>;
}

// What the page gets. It sends folder and nonce to the server; the token never leaves the bridge.
export interface Prepared {
  folder: string;
  mode: Mode;
  nonce: string;
  token: string;
}

export interface BinderOptions {
  bindings: Bindings;
  prompts: FolderPrompts;
  guards: FolderGuards;
  agent: string;
  hosts: Executor; // runs everything but the binding
  // Whether the hosts give a project's thread a copy of its folder to work in: without, a bind that asks for one is refused.
  keepsCopies?: boolean;
  refusal?(operation: Operation): Outcome | null; // what the hosts refuse anyway, before anyone is asked
  retired?(root: string): void; // a deleted chat's root: the hosts let go of what they keep for it
  // The page a session's next browser operation acts in, for its prompt; for an upload, the frame of the file input
  // that asked, *of* being the upload's operation; *root*, the chat that asks.
  address?(session: string, upload?: boolean, of?: string, root?: string): Promise<string | { refused: string }>;
  notComing?(of: string): void; // an upload the browser was asked about, by its operation, that got no leave
  // Whether something in a chat's sandbox listens on a port of its own loopback now: asked before its browser is sent there.
  listening?(root: string, port: number): Promise<boolean | "busy">;
  // The user is asked about every other operation first in a chat that asks every time,
  // and about a network destination off the package hosts in either mode.
  approvalPrompts: ApprovalPrompts;
  preparedMs?: number;
  // How Show folder looks at a folder, and how long it waits; node:fs's lstat and LOOK_MS unless a test says.
  look?: (path: string) => Promise<FolderLook>;
  lookMs?: number;
  // A binding, a "Stop asking" or a host allowed for the session that could not be recorded,
  // or a network prompt that failed, and why.
  onError?: (error: unknown) => void;
}

interface Preparation {
  nonce: string;
  made?: true; // its folder was made for it: gone again, if still empty, once it binds no chat
  expiry?: ReturnType<typeof setTimeout>;
  folder: string;
  dev: number;
  ino: number;
  mode: Mode;
  window: string;
  root?: string; // the chat its bind operation bound
  settled: Promise<Binding>;
  resolve: (binding: Binding) => void;
  reject: (error: Error) => void;
}

const token = () => randomBytes(32).toString("base64url");

// The thread a bind's history names, or null for one that names none: a thread is a session's id,
// by which its copy's folder is named in the app's data.
function threadOf(history: unknown): string | null {
  const thread = typeof history === "object" && history !== null && !Array.isArray(history) ? (history as { thread?: unknown }).thread : undefined;
  return typeof thread === "string" && THREAD.test(thread) ? thread : null;
}

const day = (now: Date) =>
  `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;

// A folder made for a chat that did not take it goes, unless something was put in it meanwhile.
function removeEmpty(folder: string): void {
  try {
    rmdirSync(folder);
  } catch {
    // Not empty, or gone: it stays as it is.
  }
}

// A new chat's own folder, when there is no last one to offer (spec, Section 8): under
// ~/Surogate/<agent>, named for the day, resolved. Null when none can be made. The agent's
// name is made one folder name: a host holds no "/", but an IPv6 one holds "[" and "]",
// which no chat's folder may; nor does it start with "." or "-", hidden or read as an option.
function newFolder(guards: FolderGuards, agent: string): string | null {
  const name = agent.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[.-]+/, "");
  if (name === "") return null;
  const parent = join(guards.home, "Surogate", name);
  const today = day(new Date());
  try {
    mkdirSync(parent, { recursive: true });
    for (let n = 1; n <= 100; n += 1) {
      const folder = join(parent, n === 1 ? today : `${today} ${n}`);
      try {
        mkdirSync(folder);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "EEXIST") continue;
        throw error;
      }
      // One no chat may have (a link up its path into a guarded folder, say) is not offered.
      const checked = checkFolder(folder, guards);
      if (checked.ok) return checked.path;
      removeEmpty(folder);
      return null;
    }
  } catch {
    // The dialog opens instead.
  }
  return null;
}

export class Binder implements Executor {
  private readonly byNonce = new Map<string, Preparation>();
  private readonly byToken = new Map<string, Preparation>();
  // Bind operations answered, until the server acknowledges them.
  private readonly answered = new Map<string, Preparation>();
  // Built on the binder's own bindings, so the two cannot read different journals.
  readonly approvals: Approvals;

  constructor(private readonly options: BinderOptions) {
    this.approvals = new Approvals({
      bindings: options.bindings, prompts: options.approvalPrompts, agent: options.agent, address: options.address,
      notComing: options.notComing, refusal: options.refusal, listening: options.listening, onError: options.onError,
    });
  }

  /**
   * The folder for a new chat, as the user confirms it in the sheet: the last one
   * bound, if it can still be used, else a new one under ~/Surogate/<agent>; or one
   * from the folder dialog. Null when the user cancels. Every new chat asks, the last
   * folder too, so a server cannot bind a chat to a folder the user did not see.
   */
  async prepareFolder(
    choice: "last" | "pick", window: string, signal: AbortSignal, thread: ThreadLabel | null = null,
  ): Promise<Prepared | null> {
    const { guards } = this.options;
    const last = this.options.bindings.last()?.folder;
    const usable = choice === "last" && last !== undefined && checkFolder(last, guards).ok ? last : null;
    // Made for this chat, and gone again unless the user takes it, even when the sheet fails.
    const made = usable === null && choice === "last" && !signal.aborted ? newFolder(guards, this.options.agent) : null;
    let prepared: Prepared | null = null;
    try {
      prepared = await this.confirm(usable ?? made, last ?? guards.home, window, signal, thread);
      return prepared;
    } finally {
      // Taken: it goes still if its chat is never bound.
      const preparation = made !== null && prepared?.folder === made ? this.byToken.get(prepared.token) : undefined;
      if (preparation) preparation.made = true;
      else if (made !== null) this.release(made);
    }
  }

  /** What the page may know of a chat's folder here: where it is, and whether it asks; null for a chat with none on this computer. */
  bindingOf(sessionId: string): { folder: string; mode: Mode } | null {
    const binding = this.options.bindings.get(sessionId);
    // A mode it does not know asks, as the approvals take it.
    return binding ? { folder: binding.folder, mode: binding.mode === "free" ? "free" : "ask" } : null;
  }

  /**
   * The chat's folder, for the file manager to show, while it is still the folder its user
   * confirmed: one replaced since, by another folder, a link or a file, is refused. The user waits
   * LOOK_MS at most. Node cannot cancel a look, and one into a dead mount holds one of libuv's four
   * threads until it returns, which the main process's other file calls and lookups then wait on: so
   * a chat has one look at a time, and the process LOOKS_AT_ONCE.
   */
  async folderToShow(sessionId: string): Promise<string> {
    const binding = this.options.bindings.get(sessionId);
    if (!binding) throw new Error("This chat has no folder on this computer");
    if (looking.has(sessionId)) throw new Error(`Surogate is still looking for ${binding.folder}`);
    if (looking.size >= LOOKS_AT_ONCE) throw new Error("Surogate is still looking for another folder");
    looking.add(sessionId);
    // lstat: a link at its path is never the folder. The path was resolved when it was bound,
    // so only its last name can have become a link since.
    const look = (this.options.look ?? lstat)(binding.folder).then((found) => found, () => null);
    void look.finally(() => looking.delete(sessionId));
    const ms = this.options.lookMs ?? LOOK_MS;
    let timer: NodeJS.Timeout | undefined;
    const found = await Promise.race([
      look,
      new Promise<"late">((resolve) => {
        timer = setTimeout(() => resolve("late"), ms).unref();
      }),
    ]).finally(() => clearTimeout(timer));
    if (found === "late") throw new Error(`The folder ${binding.folder} did not answer within ${ms / 1000} s`);
    if (!found) throw new Error(`The folder ${binding.folder} is not there`);
    if (!found.isDirectory() || !confirmedFolder(binding, found)) {
      throw new Error(`The folder ${binding.folder} was replaced after it was confirmed for this chat`);
    }
    return binding.folder;
  }

  /** Drop a folder confirmed in *window* whose chat was never created: its bind is refused from now on. A chat bound already keeps it. */
  cancelPrepared(preparedToken: string, window: string): void {
    const preparation = this.byToken.get(preparedToken);
    if (!preparation || preparation.window !== window || preparation.root !== undefined) return;
    this.byToken.delete(preparedToken);
    this.unbound(preparation);
    preparation.reject(new Error("This folder's confirmation was dropped before its chat was created"));
  }

  // A confirmation that binds no chat, dropped or expired: it can bind none from now on, and
  // a folder made for it is let go, once.
  private unbound(preparation: Preparation): void {
    clearTimeout(preparation.expiry);
    this.byNonce.delete(preparation.nonce);
    if (!preparation.made) return;
    delete preparation.made;
    this.release(preparation.folder);
  }

  // A folder made for a chat that took none goes, if still empty, unless a chat holds it by
  // now: the same day's path is made again for the next chat once it has gone, and a user may
  // pick it for another.
  private release(folder: string): void {
    try {
      const open = [...this.byNonce.values()].some((preparation) => preparation.folder === folder);
      if (!open && !this.options.bindings.folders().includes(folder)) removeEmpty(folder);
    } catch (error) {
      // A journal that cannot be read keeps it.
      report(this.options.onError, error);
    }
  }

  // The sheet for *offered*, or for the folder the dialog picks from *startIn*, until the user accepts one or cancels.
  private async confirm(
    offered: string | null, startIn: string, window: string, signal: AbortSignal, thread: ThreadLabel | null,
  ): Promise<Prepared | null> {
    const { prompts, guards } = this.options;
    // A page that has gone gets no prompt: the signal is looked at before each one.
    let folder = offered ?? (signal.aborted ? null : await prompts.pickFolder(startIn, signal));
    while (folder !== null && !signal.aborted) {
      const checked = checkFolder(folder, guards);
      const links = checked.ok ? await scanLinks(checked.path) : null;
      if (signal.aborted) return null;
      const sheet: FolderSheet = {
        agent: this.options.agent,
        folder: checked.ok ? checked.path : folder,
        mode: "free",
        links,
        refusal: checked.ok ? null : checked.message,
        thread,
      };
      const answer = await prompts.confirmFolder(sheet, signal);
      if (answer === null || signal.aborted) return null;
      if (answer !== "change") return checked.ok ? this.prepare(checked, answer.mode, window) : null;
      // A dialog the user cancels goes back to the sheet.
      folder = (await prompts.pickFolder(sheet.folder, signal)) ?? folder;
    }
    return null;
  }

  /**
   * Resolves with the chat's binding once the server has recorded it, so the
   * page's first message is never refused as "still being set up". The bind
   * operation may come before or after this call; a repeat is answered the same.
   */
  async bindSession(sessionId: string, preparedToken: string, window: string): Promise<Binding> {
    const preparation = this.byToken.get(preparedToken);
    if (!preparation || preparation.window !== window) throw new Error("This folder was not confirmed in this window");
    // One that failed before it bound a chat is told once, then forgotten.
    const binding = await preparation.settled.catch((error: unknown) => {
      if (preparation.root === undefined) this.byToken.delete(preparedToken);
      throw error;
    });
    if (binding.root !== sessionId) throw new Error("This folder was confirmed for another chat");
    return binding;
  }

  // The binding is made when its operation arrives, before the journal marks it
  // started: an app that stops between recording it and answering finds it here
  // when the server sends the operation again. It touches no file: the host checks
  // the folder against the binding's identity before any work. A bind waits on
  // nothing, so it settles at once, an aborted one too: suspend waits for it. Every
  // other operation is the approvals', which settle once the signal aborts. *download*: a write
  // the desktop makes itself to save one, and whose it is (browser/downloads.ts).
  async admit(operation: Operation, signal: AbortSignal, download?: DownloadBy): Promise<Outcome | null> {
    if (operation.kind === "retire") return this.retire(operation);
    // What the tools refuse anyway (no browser on this computer, say) is refused before anyone is
    // asked; in the same tick, so a chat's bind in the same burst cannot slip in before the approvals look.
    if (operation.kind !== "bind") return this.options.refusal?.(operation) ?? this.approvals.admit(operation, signal, download);
    const root = operation.sessionId;
    const { folder, nonce, history } = operation.args;
    const own = operation.callingSessionId === root && operation.invocationId === "bind" && operation.ordinal === 0;
    if (!own || typeof folder !== "string" || typeof nonce !== "string") return NOT_BOUND;
    // A project's thread is bound to a copy of the folder (spec, Section 13): the server asks for
    // one by naming the thread, which is the root itself. A root is never given another's copy,
    // and one asked for is never answered with the folder itself: the answer names the copy it
    // recorded, or refuses.
    const copy = history === undefined ? undefined : threadOf(history);
    if (copy === null || (copy !== undefined && copy !== root)) return NOT_BOUND;
    const known = this.options.bindings.get(root);
    if (known) return known.nonce === nonce && known.folder === folder && known.history === copy ? boundTo(known.history) : ALREADY_BOUND;
    const preparation = this.byNonce.get(nonce);
    if (!preparation) return NOT_BOUND;
    // One use, whatever the answer.
    this.byNonce.delete(nonce);
    if (folder !== preparation.folder) {
      preparation.reject(new Error("The server named another folder for this chat"));
      return NOT_BOUND;
    }
    // Nothing here would make the thread's copy: it is bound to nothing, so that it works nowhere.
    if (copy !== undefined && this.options.keepsCopies !== true) {
      preparation.reject(new Error(NO_COPY_HERE));
      return KEEPS_NO_COPY;
    }
    const { dev, ino, mode } = preparation;
    try {
      this.options.bindings.add({ root, nonce, folder, dev, ino, boot: BOOT_ID, mode, boundAt: Date.now(), ...(copy === undefined ? {} : { history: copy }) });
    } catch (error) {
      report(this.options.onError, error);
      preparation.reject(error instanceof Error ? error : new Error(String(error)));
      return NOT_RECORDED;
    }
    preparation.root = root;
    this.answered.set(operation.id, preparation);
    return boundTo(copy);
  }

  // A deleted chat's root: its binding goes, so nothing more runs for it; its folder stays as it is.
  private retire(operation: Operation): Outcome {
    const own = operation.callingSessionId === operation.sessionId && operation.invocationId === "retire" && operation.ordinal === 0;
    if (!own) return NOT_BOUND;
    // Its tabs close whether or not its binding can be forgotten: the chat is gone.
    this.options.retired?.(operation.sessionId);
    try {
      this.options.bindings.retire(operation.sessionId);
    } catch (error) {
      report(this.options.onError, error);
      return NOT_FORGOTTEN;
    }
    return BOUND;
  }

  // Every op_ack comes here: one for no bind answered in this run, or one already
  // acknowledged, changes nothing.
  acknowledged(id: string): void {
    const preparation = this.answered.get(id);
    if (preparation?.root === undefined) return;
    this.answered.delete(id);
    const binding = this.options.bindings.get(preparation.root);
    if (binding) preparation.resolve(binding);
  }

  async run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    const outcome = await this.options.hosts.run(operation, signal);
    // A prompt before input to a background process names the command it runs.
    this.approvals.started(operation, outcome);
    return outcome;
  }

  async end(): Promise<void> {
    // No op_ack comes once access has ended, so a page waiting for one is told now.
    for (const preparation of this.answered.values()) {
      preparation.reject(new Error("This computer's access to the agent ended before its chat's folder was recorded"));
    }
    this.answered.clear();
    await this.options.hosts.end?.();
  }

  private prepare(checked: { path: string; dev: number; ino: number }, mode: Mode, window: string): Prepared {
    const nonce = token();
    const secret = token();
    const { promise: settled, resolve, reject } = Promise.withResolvers<Binding>();
    // Nobody may wait for it.
    settled.catch(() => {});
    const preparation: Preparation = {
      nonce, folder: checked.path, dev: checked.dev, ino: checked.ino, mode, window, settled, resolve, reject,
    };
    this.byNonce.set(nonce, preparation);
    this.byToken.set(secret, preparation);
    // ponytail: a preparation stays for the app's life, so a repeated bindSession is
    // answered, until bindSession hears it failed before binding a chat; one per folder
    // confirmed in this run.
    preparation.expiry = setTimeout(() => {
      if (preparation.root !== undefined) return;
      this.unbound(preparation);
      reject(new Error("This folder's confirmation expired before its chat was created"));
    }, this.options.preparedMs ?? PREPARED_MS).unref();
    return { folder: checked.path, mode, nonce, token: secret };
  }
}

// The binding handshake (spec, Section 8). The page asks for a folder, and the
// user confirms one in the desktop's own sheet; the page creates the chat with
// the confirmation's nonce; the server sends that chat's bind operation, which is
// answered as it arrives, against the confirmation; bindSession tells the page
// once the server has recorded the answer. Every other operation is put to the
// approvals first (approvals.ts), then goes to the tool hosts, which find their
// folder in the bindings.

import { randomBytes } from "node:crypto";
import { mkdirSync, rmdirSync } from "node:fs";

import { join } from "../host-path.js";
import { hostLooked } from "../host-stat.js";
import { guestPathOf, hostPathOf, shownPath } from "../session-path.js";
import { type NoCopyAt, noCopyAt } from "../hosts/start.js";
import { NOT_BOUND } from "../hosts/tool-hosts.js";
import type { Binding, Bindings, Mode } from "../journal/bindings.js";
import type { Operation, Outcome } from "../link/protocol.js";
import type { Executor } from "../operations/runner.js";
import { report } from "../report.js";
import { THREAD } from "../vm/history.js";
import { type ApprovalPrompts, Approvals, type DownloadBy } from "./approvals.js";
import { BOOT_ID, checkFolder, confirmedFolder, type FolderCheck, type FolderGuards } from "./folder.js";
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
// A project's thread on *folder*, which no copy of it could stand for by its path (hosts/start.ts): it works nowhere, and the
// person is told to choose another folder.
const noCopyOf = (folder: string, why: NoCopyAt): string => {
  const reason = "given" in why ? `every sandbox of a thread's copy reads all of ${why.given}, which holds it or lies in it` : why.spelled;
  return `The folder ${folder} cannot have a project's thread work in a copy of it, as ${reason}: choose another folder for the project's threads`;
};
// What a project's thread says where its project's folder can no longer be worked in here, for its user to choose it again.
const CHOOSE_AGAIN = "choose Use this computer for this project in its Overview, then start this thread again";
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
  project: string | null; // a new project the folder is for, by the name it has so far ("" before it has one); null for a chat or a thread
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
  // How Show folder looks at a folder, by the session's path, and how long it waits; its lstat by this computer's own
  // path (host-stat.ts) and LOOK_MS unless a test says.
  look?: (path: string) => Promise<FolderLook>;
  lookMs?: number;
  // Whether a folder for a new chat is there and answered a look off this thread in time (FolderLooks): its checks
  // look at it on this thread, which one on a mount that has stopped answering would stop. Without it, each is taken as answering.
  answers?: (folder: string) => Promise<boolean>;
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
  project?: true; // a project's own folder, which its coordinator binds and its threads' copies are made of
  master?: string; // that project's coordinator, where its folder is chosen again: it binds that chat alone, afresh
  thread?: true; // a copy of a project's folder for one of its threads, which no sheet asked: it binds no chat to the folder itself
  root?: string; // the chat its bind operation bound
  settled: Promise<Binding>;
  resolve: (binding: Binding) => void;
  reject: (error: Error) => void;
}

// What a confirmation is for, beyond a chat's folder: a project's own folder, and its coordinator where it is chosen
// again; or a thread's copy of a project's folder.
type Marks = Pick<Preparation, "project" | "master" | "thread">;

const token = () => randomBytes(32).toString("base64url");

// The thread a bind's history names, or null for one that names none: a thread is a session's id,
// by which its copy's folder is named in the app's data.
function threadOf(history: unknown): string | null {
  const thread = typeof history === "object" && history !== null && !Array.isArray(history) ? (history as { thread?: unknown }).thread : undefined;
  return typeof thread === "string" && THREAD.test(thread) ? thread : null;
}

const day = (now: Date) =>
  `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;

// A folder made for a chat that did not take it goes, unless something was put in it meanwhile. By the session's path.
function removeEmpty(folder: string): void {
  try {
    rmdirSync(hostPathOf(folder));
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
      const checked = checkFolder(guestPathOf(folder), guards);
      if (checked.ok) return checked.path;
      removeEmpty(guestPathOf(folder));
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
    const usable = choice === "last" && last !== undefined && (await this.answers(last)) && checkFolder(last, guards).ok ? last : null;
    // Made for this chat, and gone again unless the user takes it, even when the sheet fails.
    const made = usable === null && choice === "last" && !signal.aborted ? newFolder(guards, this.options.agent) : null;
    let prepared: Prepared | null = null;
    try {
      prepared = await this.confirm(usable ?? made, last ?? guestPathOf(guards.home), window, signal, thread, null);
      return prepared;
    } finally {
      // Taken: it goes still if its chat is never bound.
      const preparation = made !== null && prepared?.folder === made ? this.byToken.get(prepared.token) : undefined;
      if (preparation) preparation.made = true;
      else if (made !== null) this.release(made);
    }
  }

  /**
   * The folder for a new project made here, as the user picks it in the folder dialog and confirms it in the sheet,
   * which names *project*: its coordinator works in the folder itself, and each of its threads in a copy of it,
   * which asks no more (prepareThreadFolder). Null when the user cancels. With *again*, the folder of a project whose
   * coordinator, *again.master*, can no longer work in the one it had here (projectFolderLost), or works on another
   * computer: the sheet opens on its own path, and any other may be chosen; it binds that coordinator alone, afresh.
   */
  async prepareProjectFolder(
    window: string, signal: AbortSignal, project: string, again?: { master: string; folder: string | null },
  ): Promise<Prepared | null> {
    const last = this.options.bindings.last()?.folder;
    const offered = again?.folder ?? null;
    return this.confirm(offered, last ?? guestPathOf(this.options.guards.home), window, signal, null, project.trim(), { project: true, ...(again ? { master: again.master } : {}) });
  }

  /**
   * A copy of a project's folder for one of its threads, with no dialog and no sheet: the folder its user chose for
   * the project here, which its coordinator's chat, *master*, works in. The thread asks as that chat does, and the
   * confirmation binds it to a copy alone. Refused, saying how the project works here again, for a chat whose folder
   * was confirmed for no project, and for a folder that is no longer the one its user chose; and where no copy of it
   * could be made, before the thread is.
   */
  async prepareThreadFolder(master: string, window: string): Promise<Prepared> {
    const found = await this.projectFolder(master);
    if (typeof found === "string") throw new Error(`${found}: ${CHOOSE_AGAIN}`);
    if (this.options.keepsCopies !== true) throw new Error(NO_COPY_HERE);
    const { binding, checked } = found;
    const alone = noCopyAt(binding.folder, this.options.guards);
    if (alone !== null) throw new Error(noCopyOf(shownPath(binding.folder), alone));
    return this.prepare(checked, binding.mode === "free" ? "free" : "ask", window, { thread: true });
  }

  /**
   * Why a project's coordinator, *master*, can no longer work in its folder here, for its user to choose the folder
   * again (prepareProjectFolder): this computer holds no binding of it as a project's, or its folder is not there,
   * or is no longer the one its user chose. Null while it can.
   */
  async projectFolderLost(master: string): Promise<string | null> {
    const found = await this.projectFolder(master);
    return typeof found === "string" ? found : null;
  }

  // The project's folder that *master* works in here, as its user chose it, or why there is none.
  private async projectFolder(master: string): Promise<{ binding: Binding; checked: FolderCheck & { ok: true } } | string> {
    const binding = this.options.bindings.get(master);
    if (!binding?.project) return "This project's folder is not set up on this computer";
    const { folder } = binding;
    // Its words are its person's: each folder by the path they read it by (session-path.ts, shownPath).
    const checked: FolderCheck = (await this.answers(folder))
      ? checkFolder(folder, this.options.guards, shownPath)
      : { ok: false, missing: true, message: `the folder ${shownPath(folder)} did not answer` };
    if (!checked.ok) return `This project's folder cannot be used: ${checked.message}`;
    if (checked.path !== folder || !confirmedFolder(binding, checked)) return `The folder ${shownPath(folder)} was replaced after it was chosen for this project`;
    return { binding, checked };
  }

  private answers(folder: string): Promise<boolean> {
    return this.options.answers?.(folder) ?? Promise.resolve(true);
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
    // Its words are its person's: the folder by the path they read it by (session-path.ts, shownPath).
    const shown = shownPath(binding.folder);
    if (looking.has(sessionId)) throw new Error(`Surogate is still looking for ${shown}`);
    if (looking.size >= LOOKS_AT_ONCE) throw new Error("Surogate is still looking for another folder");
    looking.add(sessionId);
    // lstat: a link at its path is never the folder. The path was resolved when it was bound,
    // so only its last name can have become a link since.
    const look = (this.options.look ?? ((folder: string) => hostLooked(hostPathOf(folder))))(binding.folder).then((found) => found, () => null);
    void look.finally(() => looking.delete(sessionId));
    const ms = this.options.lookMs ?? LOOK_MS;
    let timer: NodeJS.Timeout | undefined;
    const found = await Promise.race([
      look,
      new Promise<"late">((resolve) => {
        timer = setTimeout(() => resolve("late"), ms).unref();
      }),
    ]).finally(() => clearTimeout(timer));
    if (found === "late") throw new Error(`The folder ${shown} did not answer within ${ms / 1000} s`);
    if (!found) throw new Error(`The folder ${shown} is not there`);
    if (!found.isDirectory() || !confirmedFolder(binding, found)) {
      throw new Error(`The folder ${shown} was replaced after it was confirmed for this chat`);
    }
    return binding.folder;
  }

  /** Whether a folder confirmed in *window* can still bind the chat it is for: neither bound, nor dropped, nor expired. */
  isPrepared(preparedToken: string, window: string): boolean {
    const preparation = this.byToken.get(preparedToken);
    return preparation?.window === window && preparation.root === undefined && this.byNonce.get(preparation.nonce) === preparation;
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

  // The sheet for *offered*, or for the folder the dialog picks from *startIn*, until the user accepts one or cancels:
  // for a chat, a project's thread, or a *project*'s own folder (*marks*).
  private async confirm(
    offered: string | null, startIn: string, window: string, signal: AbortSignal, thread: ThreadLabel | null,
    project: string | null, marks: Marks = {},
  ): Promise<Prepared | null> {
    const { prompts, guards } = this.options;
    // A page that has gone gets no prompt: the signal is looked at before each one.
    let folder = offered ?? (signal.aborted ? null : await prompts.pickFolder(startIn, signal));
    while (folder !== null && !signal.aborted) {
      // The sheet's words are its person's: the folder by the path they read it by (session-path.ts, shownPath).
      const checked: FolderCheck = (await this.answers(folder))
        ? checkFolder(folder, guards, shownPath)
        : { ok: false, missing: true, message: `the folder ${shownPath(folder)} did not answer` };
      if (signal.aborted) return null;
      const links = checked.ok ? await scanLinks(checked.path) : null;
      if (signal.aborted) return null;
      const sheet: FolderSheet = {
        agent: this.options.agent,
        folder: checked.ok ? checked.path : folder,
        mode: "free",
        links,
        refusal: checked.ok ? null : checked.message,
        thread,
        project,
      };
      const answer = await prompts.confirmFolder(sheet, signal);
      if (answer === null || signal.aborted) return null;
      if (answer !== "change") return checked.ok ? this.prepare(checked, answer.mode, window, marks) : null;
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
    if (known?.nonce === nonce && known.folder === folder && known.history === copy) return boundTo(known.history);
    const preparation = this.byNonce.get(nonce);
    // A project's coordinator whose folder its user chose again is bound afresh, its binding here replaced; no other
    // chat that has one is bound again.
    const again = known?.project === true && preparation?.master === root;
    if (known && !again) return ALREADY_BOUND;
    if (!preparation) return NOT_BOUND;
    // One use, whatever the answer.
    this.byNonce.delete(nonce);
    if (folder !== preparation.folder) {
      preparation.reject(new Error("The server named another folder for this chat"));
      return NOT_BOUND;
    }
    // A project's folder chosen again is its own coordinator's, never another chat's.
    if (preparation.master !== undefined && preparation.master !== root) {
      preparation.reject(new Error("The server named another chat for this project's folder"));
      return NOT_BOUND;
    }
    // A project's own folder is its coordinator's, never a thread's copy.
    if (copy !== undefined && preparation.project) {
      preparation.reject(new Error("The server asked for a copy of a project's own folder"));
      return NOT_BOUND;
    }
    // And a thread's copy of it, which no sheet asked, is never the folder itself.
    if (copy === undefined && preparation.thread) {
      preparation.reject(new Error("The server asked for a project's folder itself for a thread"));
      return NOT_BOUND;
    }
    // Nothing here would make the thread's copy: it is bound to nothing, so that it works nowhere.
    if (copy !== undefined && this.options.keepsCopies !== true) {
      preparation.reject(new Error(NO_COPY_HERE));
      return KEEPS_NO_COPY;
    }
    // Nor where no host on its copy could stand for the folder by its path, as each one's start would refuse it.
    const alone = copy === undefined ? null : noCopyAt(folder, this.options.guards);
    if (alone !== null) {
      const message = noCopyOf(folder, alone);
      preparation.reject(new Error(message));
      return { error: { type: "binding", message } };
    }
    const { dev, ino, mode } = preparation;
    try {
      const binding: Binding = {
        root, nonce, folder, dev, ino, boot: BOOT_ID, mode, boundAt: Date.now(),
        ...(copy === undefined ? {} : { history: copy }), ...(preparation.project ? { project: true } : {}),
      };
      if (again) this.options.bindings.replace(binding);
      else this.options.bindings.add(binding);
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

  private prepare(checked: { path: string; dev: number; ino: number }, mode: Mode, window: string, marks: Marks = {}): Prepared {
    const nonce = token();
    const secret = token();
    const { promise: settled, resolve, reject } = Promise.withResolvers<Binding>();
    // Nobody may wait for it.
    settled.catch(() => {});
    const preparation: Preparation = {
      nonce, folder: checked.path, dev: checked.dev, ino: checked.ino, mode, window, settled, resolve, reject, ...marks,
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

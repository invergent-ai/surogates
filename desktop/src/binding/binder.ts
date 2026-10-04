// The binding handshake (spec, Section 8). The page asks for a folder, and the
// user confirms one in the desktop's own sheet; the page creates the chat with
// the confirmation's nonce; the server sends that chat's bind operation, which is
// answered as it arrives, against the confirmation; bindSession tells the page
// once the server has recorded the answer. Every other operation is put to the
// approvals first (approvals.ts), then goes to the tool hosts, which find their
// folder in the bindings.

import { randomBytes } from "node:crypto";

import { NOT_BOUND } from "../hosts/tool-hosts.js";
import type { Binding, Bindings, Mode } from "../journal/bindings.js";
import type { Operation, Outcome } from "../link/protocol.js";
import type { Executor } from "../operations/runner.js";
import { report } from "../report.js";
import { type ApprovalPrompts, Approvals } from "./approvals.js";
import { BOOT_ID, checkFolder, type FolderGuards } from "./folder.js";
import { type LinkSummary, scanLinks } from "./links.js";

// How long a confirmed folder waits for its chat's bind operation.
export const PREPARED_MS = 5 * 60_000;

// The server appends ". Start a new chat." to a binding refusal's message: none ends in ".".
export const ALREADY_BOUND: Outcome = {
  error: { type: "binding", message: "This chat already works on another folder of this computer" },
};
// The binding could not be written to the journal (a full disk, say).
export const NOT_RECORDED: Outcome = {
  error: { type: "binding", message: "This computer could not record the folder for this chat" },
};
const BOUND: Outcome = { ok: null };

// What the sheet shows. Accepting binds *folder* with the mode chosen there.
export interface FolderSheet {
  agent: string; // the agent the chat is with
  folder: string; // resolved
  mode: Mode; // as the sheet opens
  links: LinkSummary | null; // null: no file in the folder is linked from elsewhere
  refusal: string | null; // why no chat may work on this folder: the sheet offers Change and Cancel only
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
  // The user is asked about every other operation first in a chat that asks every time,
  // and about a network destination off the package hosts in either mode.
  approvalPrompts: ApprovalPrompts;
  preparedMs?: number;
  // A binding, a "Stop asking" or a host allowed for the session that could not be recorded,
  // or a network prompt that failed, and why.
  onError?: (error: unknown) => void;
}

interface Preparation {
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

export class Binder implements Executor {
  private readonly byNonce = new Map<string, Preparation>();
  private readonly byToken = new Map<string, Preparation>();
  // Bind operations answered, until the server acknowledges them.
  private readonly answered = new Map<string, Preparation>();
  // Built on the binder's own bindings, so the two cannot read different journals.
  readonly approvals: Approvals;

  constructor(private readonly options: BinderOptions) {
    this.approvals = new Approvals({
      bindings: options.bindings, prompts: options.approvalPrompts, agent: options.agent, onError: options.onError,
    });
  }

  /**
   * The folder for a new chat, as the user confirms it in the sheet: the last one
   * bound, if it can still be used, or one from the folder dialog. Null when the
   * user cancels. Every new chat asks, the last folder too, so a server cannot bind
   * a chat to a folder the user did not see.
   */
  async prepareFolder(choice: "last" | "pick", window: string, signal: AbortSignal): Promise<Prepared | null> {
    const { prompts, guards } = this.options;
    const last = this.options.bindings.last()?.folder;
    // A page that has gone gets no prompt: the signal is looked at before each one.
    let folder = choice === "last" && last !== undefined && checkFolder(last, guards).ok ? last : null;
    folder ??= signal.aborted ? null : await prompts.pickFolder(last ?? guards.home, signal);
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
  // other operation is the approvals', which settle once the signal aborts.
  async admit(operation: Operation, signal: AbortSignal): Promise<Outcome | null> {
    if (operation.kind !== "bind") return this.approvals.admit(operation, signal);
    const root = operation.sessionId;
    const { folder, nonce } = operation.args;
    const own = operation.callingSessionId === root && operation.invocationId === "bind" && operation.ordinal === 0;
    if (!own || typeof folder !== "string" || typeof nonce !== "string") return NOT_BOUND;
    const known = this.options.bindings.get(root);
    if (known) return known.nonce === nonce && known.folder === folder ? BOUND : ALREADY_BOUND;
    const preparation = this.byNonce.get(nonce);
    if (!preparation) return NOT_BOUND;
    // One use, whatever the answer.
    this.byNonce.delete(nonce);
    if (folder !== preparation.folder) {
      preparation.reject(new Error("The server named another folder for this chat"));
      return NOT_BOUND;
    }
    const { dev, ino, mode } = preparation;
    try {
      this.options.bindings.add({ root, nonce, folder, dev, ino, boot: BOOT_ID, mode, boundAt: Date.now() });
    } catch (error) {
      report(this.options.onError, error);
      preparation.reject(error instanceof Error ? error : new Error(String(error)));
      return NOT_RECORDED;
    }
    preparation.root = root;
    this.answered.set(operation.id, preparation);
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

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    return this.options.hosts.run(operation, signal);
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
      folder: checked.path, dev: checked.dev, ino: checked.ino, mode, window, settled, resolve, reject,
    };
    this.byNonce.set(nonce, preparation);
    this.byToken.set(secret, preparation);
    // ponytail: a preparation stays for the app's life, so a repeated bindSession is
    // answered, until bindSession hears it failed before binding a chat; one per folder
    // confirmed in this run.
    setTimeout(() => {
      this.byNonce.delete(nonce);
      if (preparation.root === undefined) reject(new Error("This folder's confirmation expired before its chat was created"));
    }, this.options.preparedMs ?? PREPARED_MS).unref();
    return { folder: checked.path, mode, nonce, token: secret };
  }
}

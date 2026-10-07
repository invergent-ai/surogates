// Approvals (spec, Section 4). A chat that asks every time has its user allow
// each command, file change and process input before it happens, in the
// desktop's own window; a chat that works freely is asked nothing here. Each is
// asked in the runner's admit phase, before the journal marks the operation
// started: a prompt still open when the app stops is asked again once the server
// sends the operation again, nothing ran, and the command's timeout starts only
// once it is allowed. Only the desktop makes a chat less safe: the page may switch
// a chat to Ask every time, and Work freely needs the desktop's own confirmation,
// or "Stop asking" on one of its prompts. In either mode, a command's connection to
// a destination off the package hosts asks too, while the command runs; "Allow for
// this session" lets that host through, on every port, for the rest of the chat.

import { posix } from "node:path";

import { FOLDER_UNAVAILABLE, type NetworkAnswer, type NetworkAsk } from "../hosts/messages.js";
import type { Binding, Bindings } from "../journal/bindings.js";
import type { Operation, Outcome } from "../link/protocol.js";
import { report } from "../report.js";

// What Ask every time never asks about: these only read, or make things safer (kill).
// Every other kind asks (run, start, write, delete, write_stdin), and so does a new one.
export const UNASKED: ReadonlySet<string> = new Set([
  "resolve", "check_write", "stat", "read", "read_lines", "list_dir", "ripgrep", "which", "poll", "read_output", "wait",
  "kill", "list_processes",
]);

// The harness's own files: the terminal spills long output here.
const RESULTS = ".surogates-results";

// The chat a prompt is for, and the session asking: a sub-agent of the chat when it is not the root.
// A network prompt names the root: srt does not say which session's command asked.
export interface ChatLabel {
  agent: string;
  root: string;
  calling: string;
  folder: string;
}

// The head of a write's new data, as text: cut when the data goes on past it.
export interface Preview {
  text: string;
  cut: boolean;
}

export type ApprovalRequest =
  | { kind: "command"; chat: ChatLabel; command: string; workdir: string | null; background: boolean }
  // preview: null for a delete, and for data that is not text.
  | { kind: "change"; chat: ChatLabel; action: "write" | "delete"; path: string; bytes: number | null; preview: Preview | null }
  // command: what the process runs, as its start named it; null for a process this run of the app did not start.
  | { kind: "input"; chat: ChatLabel; process: string; command: string | null; data: string }
  | { kind: "network"; chat: ChatLabel; host: string; port: number; privateNetwork: boolean };

// Allow it this once; deny it; allow it and stop asking in this chat, which then works
// freely (not offered for a network prompt); or, for a network prompt only, allow its
// host, on every port, for the rest of the chat. "timeout": nobody answered in time.
// Any answer a prompt does not offer denies.
export type ApprovalAnswer = "allow" | "deny" | "stop_asking" | "allow_session" | "timeout";

// The desktop shell's own windows; fakes in tests. Each settles once its signal aborts.
export interface ApprovalPrompts {
  // One operation or network destination, focus on Deny. Each settles once its signal aborts.
  // An operation allows on "allow" or "stop_asking"; a network prompt allows on "allow" or
  // "allow_session", and "allow_session" keeps the host for the chat, on every port. Any other answer denies.
  approve(request: ApprovalRequest, signal: AbortSignal): Promise<ApprovalAnswer>;
  // The desktop's own confirmation before a chat works freely. Only true lets it.
  confirmFreeMode(chat: ChatLabel, signal: AbortSignal): Promise<boolean>;
}

export interface ApprovalsOptions {
  bindings: Bindings;
  prompts: ApprovalPrompts;
  agent: string; // the agent's name, for the prompts
  onError?: (error: unknown) => void; // a choice that could not be recorded, or a network prompt that failed, and why
}

const DENIED = {
  command: "The user denied this command on this computer",
  change: "The user denied this change on this computer",
  input: "The user denied this input on this computer",
} as const;

// Not denied: nobody was there to answer.
const UNANSWERED = {
  command: "Nobody answered on this computer in time, so the command did not run",
  change: "Nobody answered on this computer in time, so the change was not made",
  input: "Nobody answered on this computer in time, so the input was not sent",
} as const;

// How much of a write's new data its prompt shows, read from the head of its base64.
export const PREVIEW_BYTES = 8 * 1024;
const PREVIEW_BASE64 = Math.ceil(PREVIEW_BYTES / 3) * 4;

const couldNotAsk = (error: unknown) =>
  `This computer could not ask its user about this: ${error instanceof Error ? error.message : String(error)}`;

// What each kind's prompt is about: a change, an input, or (any other kind) a command.
const shapeOf = (kind: string): keyof typeof DENIED =>
  kind === "write" || kind === "delete" ? "change" : kind === "write_stdin" ? "input" : "command";

// An operation that did not happen, in the shape its tool reads that way
// (surogates/devices/workspace.py): a command is blocked (WorkspaceSandboxError),
// a change fails with a PermissionError, an input as a write that failed.
function denied(kind: string, why: string = DENIED[shapeOf(kind)]): Outcome {
  const shape = shapeOf(kind);
  if (shape === "change") return { error: { type: "os", code: "EACCES", message: why } };
  if (shape === "input") return { ok: { status: "error", error: why } };
  return { error: { type: "sandbox", message: why } };
}

// The head of a write's data as text, or null for data that is not: a NUL, or not UTF-8.
function previewOf(data: string, bytes: number): Preview | null {
  const head = Buffer.from(data.slice(0, PREVIEW_BASE64), "base64").subarray(0, PREVIEW_BYTES);
  if (head.includes(0)) return null;
  try {
    // A character the head cuts in two is left out, not refused.
    return { text: new TextDecoder("utf-8", { fatal: true }).decode(head, { stream: true }), cut: bytes > head.length };
  } catch {
    return null;
  }
}

// What the prompt shows. The hosts take only text in these fields and refuse
// anything else before it runs; here it is shown as String() writes it. A write
// whose data came in a transfer carries it by now: the runner asks once it is whole.
function requestFor(operation: Operation, binding: Binding, agent: string, command: (process: string) => string | null): ApprovalRequest {
  const { kind, args } = operation;
  const chat = { agent, root: binding.root, calling: operation.callingSessionId, folder: binding.folder };
  const text = (name: string) => String(args[name] ?? "");
  if (kind === "delete") return { kind: "change", chat, action: kind, path: text("key"), bytes: null, preview: null };
  if (kind === "write") {
    const bytes = Buffer.byteLength(text("data"), "base64");
    return { kind: "change", chat, action: kind, path: text("key"), bytes, preview: previewOf(text("data"), bytes) };
  }
  if (kind === "write_stdin") {
    const process = text("session_id");
    return { kind: "input", chat, process, command: command(process), data: text("data") };
  }
  const workdir = args.workdir == null ? null : String(args.workdir);
  return { kind: "command", chat, command: text("command"), workdir, background: kind === "start" };
}

// Settles as *promise* does, or with undefined at once when *signal* aborts.
function settled<T>(promise: Promise<T>, signal: AbortSignal): Promise<T | undefined> {
  if (signal.aborted) return Promise.resolve(undefined);
  return new Promise((resolve, reject) => {
    const aborted = () => resolve(undefined);
    signal.addEventListener("abort", aborted, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", aborted));
  });
}

export class Approvals {
  // Each chat's last prompt in line: one prompt per chat at a time, in the order asked.
  private readonly lines = new Map<string, Promise<void>>();
  // ponytail: each background command's text by its chat and handle, kept for the app's life, one per
  // background command started: background processes end with the app.
  private readonly commands = new Map<string, string>();
  // Each chat's open Work-freely confirmation: a second request hears its answer, undefined
  // when it was dropped with the page that opened it.
  private readonly freeing = new Map<string, Promise<boolean | undefined>>();
  // ponytail: the pages whose user kept a chat asking, by page and chat, for the app's life, one per
  // refusal: a page is asked no more for that chat, and a page once replaced never asks again.
  private readonly kept = new Set<string>();

  constructor(private readonly options: ApprovalsOptions) {}

  /**
   * Null lets the operation run; an outcome is its denial. Settles once *signal*
   * aborts (a cancel, a suspend), with a denial: an operation waiting its turn
   * leaves the line, and its open prompt is dismissed.
   */
  async admit(operation: Operation, signal: AbortSignal): Promise<Outcome | null> {
    if (UNASKED.has(operation.kind)) return null;
    const checked = this.asking(operation);
    if ("answer" in checked) return checked.answer;
    const root = operation.sessionId;
    // Stopped: the runner drops what this answers, and it never lets it run.
    return this.inLine(root, signal, denied(operation.kind), async () => {
      // A "Stop asking" while it waited its turn lets it through unasked.
      const again = this.asking(operation);
      if ("answer" in again) return again.answer;
      const request = requestFor(operation, again.binding, this.options.agent, (process) => this.commands.get(`${root}\0${process}`) ?? null);
      let answer: ApprovalAnswer | undefined;
      try {
        // Raced against its signal: a prompt that ignores it cannot hold a cancel or a suspend.
        answer = await settled(this.options.prompts.approve(request, signal), signal);
      } catch (error) {
        return denied(operation.kind, couldNotAsk(error));
      }
      // A dismissed prompt's answer is not its user's.
      if (signal.aborted) return denied(operation.kind);
      if (answer === "timeout") return denied(operation.kind, UNANSWERED[shapeOf(operation.kind)]);
      if (answer === "stop_asking") {
        // The user let this one run either way.
        try {
          this.options.bindings.setMode(root, "free");
        } catch (error) {
          report(this.options.onError, error);
        }
      }
      return answer === "allow" || answer === "stop_asking" ? null : denied(operation.kind);
    });
  }

  /** A background command has started: a prompt before input to its process names the command. */
  started(operation: Operation, outcome: Outcome): void {
    const handle = "ok" in outcome ? (outcome.ok as { session_id?: unknown } | null)?.session_id : undefined;
    if (operation.kind !== "start" || typeof handle !== "string") return;
    this.commands.set(`${operation.sessionId}\0${handle}`, String(operation.args.command ?? ""));
  }

  /** The hosts the chat's user allowed for the chat past the package hosts: each new tool host for it starts with these. */
  granted(root: string): string[] {
    return this.options.bindings.domains(root);
  }

  /**
   * A connection a command of the chat makes to a destination off the package
   * hosts, in either mode, asked in the chat's line; a host allowed for the session
   * goes through unasked. Settles once *signal* aborts (its host stopped), denying.
   * Fails closed: a chat this computer did not bind, a journal that cannot be read, a
   * prompt that fails, and any answer it does not offer deny it. "Allow for this
   * session" keeps the host, on every port, with the binding.
   */
  async askNetwork(root: string, asked: NetworkAsk, signal: AbortSignal): Promise<NetworkAnswer> {
    let binding: Binding | undefined;
    let granted = false;
    try {
      binding = this.options.bindings.get(root);
      // Allowed for the session already: through at once, not behind the chat's open prompt.
      granted = binding !== undefined && this.granted(root).includes(asked.host);
    } catch (error) {
      report(this.options.onError, error);
      return "deny";
    }
    if (!binding) return "deny";
    if (granted) return "allow";
    const chat = { agent: this.options.agent, root, calling: root, folder: binding.folder };
    return this.inLine(root, signal, "deny", async () => {
      // Allowed for the session while this one waited its turn: srt lets it through already.
      try {
        if (this.granted(root).includes(asked.host)) return "allow";
      } catch (error) {
        report(this.options.onError, error);
        return "deny";
      }
      let answer: ApprovalAnswer | undefined;
      try {
        answer = await settled(this.options.prompts.approve({ kind: "network", chat, ...asked }, signal), signal);
      } catch (error) {
        report(this.options.onError, error);
        return "deny";
      }
      if (signal.aborted || (answer !== "allow" && answer !== "allow_session")) return "deny";
      if (answer === "allow") return "allow";
      try {
        this.options.bindings.allowDomain(root, asked.host);
        return "allow_session";
      } catch (error) {
        // The user let these connections through either way.
        report(this.options.onError, error);
        return "allow";
      }
    });
  }

  // One prompt per chat at a time, in the order asked: *ask* runs at this one's turn,
  // or *aborted* is the answer once *signal* aborts first.
  private async inLine<T>(root: string, signal: AbortSignal, aborted: T, ask: () => Promise<T>): Promise<T> {
    const before = this.lines.get(root) ?? Promise.resolve();
    const { promise: turn, resolve: done } = Promise.withResolvers<void>();
    const mine = before.then(() => turn);
    this.lines.set(root, mine);
    // The chat's entry goes once the last in line has had its turn, not when it leaves
    // early: the one before it may still have its prompt open.
    void mine.then(() => {
      if (this.lines.get(root) === mine) this.lines.delete(root);
    });
    try {
      await settled(before, signal);
      return signal.aborted ? aborted : await ask();
    } finally {
      done();
    }
  }

  /** The page's switch, to Ask every time only: the page runs the server's content. */
  setMode(sessionId: string, mode: "ask"): void {
    if (mode !== "ask") throw new Error("Only the desktop can let a chat work freely");
    this.bound(sessionId);
    this.options.bindings.setMode(sessionId, "ask");
  }

  /**
   * Work freely, once the user confirms it in the desktop's own window. True when the
   * chat now works freely. One confirmation per chat at a time: a request while one is
   * open hears its answer. Settles false once *signal* aborts, even when the
   * confirmation does not; the confirmation opened under the first request's signal.
   * Once its user keeps the chat asking (or lets the confirmation go unanswered), *page*,
   * one load of the page that asked, is refused for that chat without asking again: a
   * page cannot wear its user down.
   */
  async requestFreeMode(sessionId: string, signal: AbortSignal, page: string): Promise<boolean> {
    const binding = this.bound(sessionId);
    if (binding.mode === "free") return true;
    const key = `${page}\0${sessionId}`;
    if (this.kept.has(key)) throw new Error("The user chose to keep this chat asking");
    if (signal.aborted) return false;
    let open = this.freeing.get(sessionId);
    if (!open) {
      const chat = { agent: this.options.agent, root: sessionId, calling: sessionId, folder: binding.folder };
      open = settled(this.options.prompts.confirmFreeMode(chat, signal), signal).then((confirmed) => {
        // Dropped with its opener's page: nobody's answer.
        if (signal.aborted) return undefined;
        if (confirmed !== true) return false;
        this.options.bindings.setMode(sessionId, "free");
        return true;
      }).finally(() => this.freeing.delete(sessionId));
      this.freeing.set(sessionId, open);
    }
    const freed = await settled(open, signal);
    // Kept asking, by its user or by nobody in time: a page that went is replaced anyway.
    if (freed === false && !signal.aborted) this.kept.add(key);
    // Freed meanwhile all the same, by "Allow and stop asking" on a prompt its confirmation waited behind.
    return freed === true || (!signal.aborted && this.bound(sessionId).mode === "free");
  }

  // The chat's binding, when this operation must be asked about now; otherwise its
  // answer, null letting it run. A journal that cannot be read denies it.
  private asking(operation: Operation): { binding: Binding } | { answer: Outcome | null } {
    let binding: Binding | undefined;
    try {
      binding = this.options.bindings.get(operation.sessionId);
    } catch (error) {
      return { answer: denied(operation.kind, couldNotAsk(error)) };
    }
    // Fail closed: the tool hosts read the binding again only when it runs, so a bind
    // arriving meanwhile must not let it run unasked.
    if (!binding) return { answer: FOLDER_UNAVAILABLE };
    // A mode it does not know asks.
    if (binding.mode === "free") return { answer: null };
    // The file helper takes only its own resolved paths as keys, so a link or a ".."
    // cannot carry a write that skips its prompt here out of this folder; a key that
    // is not already normal asks all the same.
    const key = operation.args.key;
    const spill = typeof key === "string" && posix.normalize(key) === key && key.startsWith(`${binding.folder}/${RESULTS}/`);
    if (operation.kind === "write" && spill) {
      return { answer: null };
    }
    return { binding };
  }

  private bound(sessionId: string): Binding {
    const binding = this.options.bindings.get(sessionId);
    if (!binding) throw new Error("This chat has no folder on this computer");
    return binding;
  }
}

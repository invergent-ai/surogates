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
  "resolve", "check_write", "stat", "read", "read_lines", "list_dir", "walk", "ripgrep", "which", "poll", "read_output",
  "wait", "kill", "list_processes",
]);

// Writes never asked about: under the folder at the top of a chat's folder where the
// terminal spills long output, and the chat's page saving its whiteboard's canvas, every
// few seconds while the user draws. That save is the user's own request (its invocation
// starts "request:", surogates/devices/operations.py): an agent's tool call writing the
// canvas would replace the user's board, and is asked about. Anything else under
// _whiteboard/ is asked about too: the agent could write there, and the file panel hides it.
const UNASKED_FOLDERS = [".surogates-results"];
const CANVAS = "_whiteboard/canvas.json";
const REQUEST = "request:";

// The browser's kinds (surogates/devices/browser.py). A chat's first use of the browser on this
// computer asks in either mode, and "Allow for this chat" lasts as long as the chat's binding;
// in a chat that asks every time, each one that acts on the page asks too. These do not ask:
// they read the page, close the chat's own tab, or only move or scroll the mouse. A page can
// still act on any of them, as it can on its own timers.
const BROWSER = "browser.";
const BROWSER_READS: ReadonlySet<string> = new Set(["browser.observe", "browser.screenshot", "browser.close"]);
const LOOKING: ReadonlySet<unknown> = new Set(["move", "wheel"]);
const acts = ({ kind, args }: Operation): boolean => !BROWSER_READS.has(kind) && !(kind === "browser.mouse" && LOOKING.has(args.action));

// The chat a prompt is for, and the session asking: a sub-agent of the chat when it is not the root.
// A network prompt names the root: a connection is known by its root's socket, not by which session's command made it.
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
  | { kind: "network"; chat: ChatLabel; host: string; port: number; privateNetwork: boolean }
  // The chat's first use of the browser here ("use", with no detail), or what an operation would do
  // in its page: the address it opens, the script it runs, where it clicks, what it types or presses, its drag's path.
  // page: an act's, the address of the page it acts in, as the browser said it just before; null when it did not say in time.
  | { kind: "browser"; chat: ChatLabel; action: BrowserAction; detail: string; page?: string | null };

export type BrowserAction = "use" | "open" | "script" | "click" | "type" | "press" | "drag" | "other";

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
  // The address of the page a calling session's next browser operation acts in: a page moves itself, so an act's prompt names it.
  address?: (session: string) => Promise<string>;
  onError?: (error: unknown) => void; // a choice that could not be recorded, or a network prompt that failed, and why
}

// How long an act's prompt waits for its page's address before it says the page is not known.
const ADDRESS_MS = 1_000;

const DENIED = {
  command: "The user denied this command on this computer",
  change: "The user denied this change on this computer",
  input: "The user denied this input on this computer",
} as const;

// A browser operation the user did not let happen: the tool's result says so (surogates/devices/browser.py).
const browserDenied = (message: string): Outcome => ({ error: { type: "denied", message } });
const BROWSER_DENIED = {
  use: "The user did not let the agent use the browser on this computer in this chat",
  act: "The user denied this in the agent's browser on this computer",
  unanswered: "Nobody answered on this computer in time, so the agent's browser did nothing",
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
    // A character the head cuts in two is left out, not refused; one the data itself ends in
    // the middle of is not UTF-8. A byte order mark is kept, for the page to mark.
    const cut = bytes > head.length;
    return { text: new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(head, { stream: cut }), cut };
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

// What a browser operation would do in the page, as its prompt shows it.
function browserAct({ kind, args }: Operation): { action: BrowserAction; detail: string } {
  const text = (value: unknown) => String(value ?? "");
  if (kind === "browser.navigate") return { action: "open", detail: text(args.url) };
  if (kind === "browser.evaluate") return { action: "script", detail: text(args.code) };
  if (kind === "browser.keyboard" && args.action === "press") return { action: "press", detail: text(args.keys) };
  if (kind === "browser.keyboard") {
    // Typing into a ref clicks there first: the prompt says where, after the text in quotes.
    const at = args.at as { x?: unknown; y?: unknown } | null | undefined;
    return { action: "type", detail: at ? `${JSON.stringify(text(args.text))} at ${text(at.x)}, ${text(at.y)}` : text(args.text) };
  }
  if (kind === "browser.mouse" && args.action === "drag") return { action: "drag", detail: JSON.stringify(args.path ?? []) };
  if (kind === "browser.mouse") {
    const button = args.button === undefined || args.button === "left" ? "" : ` (${text(args.button)} button)`;
    return { action: "click", detail: `${text(args.x)}, ${text(args.y)}${button}` };
  }
  return { action: "other", detail: kind };
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
    if (operation.kind.startsWith(BROWSER)) return this.browse(operation, signal);
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

  // A browser operation: the chat's first asks whether its agent may use the browser here at all,
  // and in Ask every time each that acts on the page asks too, in the chat's line. Fails closed.
  private async browse(operation: Operation, signal: AbortSignal): Promise<Outcome | null> {
    const root = operation.sessionId;
    const needed = (): { binding: Binding; use: boolean; act: boolean } | { answer: Outcome | null } => {
      let binding: Binding | undefined;
      let allowed = false;
      try {
        binding = this.options.bindings.get(root);
        allowed = binding !== undefined && this.options.bindings.browsing(root);
      } catch (error) {
        return { answer: browserDenied(couldNotAsk(error)) };
      }
      if (!binding) return { answer: FOLDER_UNAVAILABLE };
      const act = binding.mode !== "free" && acts(operation);
      return allowed && !act ? { answer: null } : { binding, use: !allowed, act };
    };
    const first = needed();
    if ("answer" in first) return first.answer;
    return this.inLine(root, signal, browserDenied(BROWSER_DENIED.act), async () => {
      // Allowed, or freed, while it waited its turn: asked no more than it still needs.
      const now = needed();
      if ("answer" in now) return now.answer;
      const chat = { agent: this.options.agent, root, calling: operation.callingSessionId, folder: now.binding.folder };
      const ask = async (request: ApprovalRequest): Promise<ApprovalAnswer | Outcome> => {
        try {
          // Raced against its signal: a prompt that ignores it cannot hold a cancel or a suspend.
          const answer = await settled(this.options.prompts.approve(request, signal), signal);
          // A dismissed prompt's answer is not its user's.
          if (signal.aborted) return browserDenied(BROWSER_DENIED.act);
          return answer === "timeout" ? browserDenied(BROWSER_DENIED.unanswered) : (answer ?? "deny");
        } catch (error) {
          return browserDenied(couldNotAsk(error));
        }
      };
      if (now.use) {
        const answer = await ask({ kind: "browser", chat, action: "use", detail: "" });
        if (typeof answer !== "string") return answer;
        if (answer !== "allow_session") return browserDenied(BROWSER_DENIED.use);
        try {
          this.options.bindings.allowBrowser(root);
        } catch (error) {
          // The user let this one through either way.
          report(this.options.onError, error);
        }
      }
      if (!now.act) return null;
      const act = browserAct(operation);
      // An open names where it goes; any other act, the page it acts in now.
      const answer = await ask(act.action === "open"
        ? { kind: "browser", chat, ...act }
        : { kind: "browser", chat, ...act, page: await this.pageOf(operation.callingSessionId, signal) });
      if (typeof answer !== "string") return answer;
      if (answer === "stop_asking") {
        try {
          this.options.bindings.setMode(root, "free");
        } catch (error) {
          report(this.options.onError, error);
        }
      }
      return answer === "allow" || answer === "stop_asking" ? null : browserDenied(BROWSER_DENIED.act);
    });
  }

  // The address of the page *session*'s act would act in, as the browser says it; null when it does not in time.
  private async pageOf(session: string, signal: AbortSignal): Promise<string | null> {
    let timer: NodeJS.Timeout | undefined;
    const late = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), ADDRESS_MS);
    });
    const said = Promise.resolve().then(() => this.options.address?.(session) ?? null).catch(() => null);
    try {
      return (await settled(Promise.race([said, late]), signal)) ?? null;
    } finally {
      clearTimeout(timer);
    }
  }

  /** A background command has started: a prompt before input to its process names the command. */
  started(operation: Operation, outcome: Outcome): void {
    // A start that ran is answered as it ran, whatever its outcome holds.
    if (operation.kind !== "start" || typeof outcome !== "object" || outcome === null || !("ok" in outcome)) return;
    const handle = (outcome.ok as { session_id?: unknown } | null)?.session_id;
    if (typeof handle !== "string") return;
    this.commands.set(`${operation.sessionId}\0${handle}`, String(operation.args.command ?? ""));
  }

  /** The hosts the chat's user allowed for the session, past the package hosts: a connection to one goes through unasked. */
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
      // Allowed for the session while this one waited its turn: a new connection would go through unasked, so this one does.
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
    const unasked = typeof key === "string" && posix.normalize(key) === key
      && (
        (key === `${binding.folder}/${CANVAS}` && operation.invocationId.startsWith(REQUEST))
        || UNASKED_FOLDERS.some((name) => key.startsWith(`${binding.folder}/${name}/`))
      );
    if (operation.kind === "write" && unasked) {
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

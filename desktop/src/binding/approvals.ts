// Approvals (spec, Section 4). A chat that asks every time has its user allow
// each command, file change and process input before it happens, in the
// desktop's own window; a chat that works freely is asked nothing here. Each is
// asked in the runner's admit phase, before the journal marks the operation
// started: a prompt still open when the app stops is asked again once the server
// sends the operation again, nothing ran, and the command's timeout starts only
// once it is allowed. Only the desktop makes a chat less safe: the page may switch
// a chat to Ask every time, and Work freely needs the desktop's own confirmation,
// or "Stop asking" on one of its prompts.

import { FOLDER_UNAVAILABLE } from "../hosts/messages.js";
import type { Binding, Bindings } from "../journal/bindings.js";
import type { Operation, Outcome } from "../link/protocol.js";

// What Ask every time asks about: whatever runs something or changes the folder.
// Every other kind reads, or makes things safer (kill).
export const ASKED: ReadonlySet<string> = new Set(["run", "start", "write", "delete", "write_stdin"]);

// The harness's own files: the terminal spills long output here.
const RESULTS = ".surogates-results";

// The chat a prompt is for, and the session asking: a sub-agent of the chat when it is not the root.
export interface ChatLabel {
  agent: string;
  root: string;
  calling: string;
  folder: string;
}

export type ApprovalRequest =
  | { kind: "command"; chat: ChatLabel; command: string; workdir: string | null; background: boolean }
  | { kind: "change"; chat: ChatLabel; action: "write" | "delete"; path: string; bytes: number | null }
  | { kind: "input"; chat: ChatLabel; process: string; data: string };

// Allow it this once; deny it; or allow it and stop asking in this chat, which then works freely.
export type ApprovalAnswer = "allow" | "deny" | "stop_asking";

// The desktop shell's own windows; fakes in tests. Each settles once its signal aborts.
export interface ApprovalPrompts {
  // One operation, focus on Deny. Any answer but "allow" or "stop_asking" denies it.
  approve(request: ApprovalRequest, signal: AbortSignal): Promise<ApprovalAnswer>;
  // The desktop's own confirmation before a chat works freely. Only true lets it.
  confirmFreeMode(chat: ChatLabel, signal: AbortSignal): Promise<boolean>;
}

export interface ApprovalsOptions {
  bindings: Bindings;
  prompts: ApprovalPrompts;
  agent: string; // the agent's name, for the prompts
}

const DENIED = {
  command: "The user denied this command on this computer",
  change: "The user denied this change on this computer",
  input: "The user denied this input on this computer",
} as const;

// An operation that did not happen, in the shape its tool reads that way
// (surogates/devices/workspace.py): a command is blocked (WorkspaceSandboxError),
// a change fails with a PermissionError, an input as a write that failed.
function denied(request: ApprovalRequest, why: string): Outcome {
  if (request.kind === "change") return { error: { type: "os", code: "EACCES", message: why } };
  if (request.kind === "input") return { ok: { status: "error", error: why } };
  return { error: { type: "sandbox", message: why } };
}

// What the prompt shows. The hosts take only text in these fields and refuse
// anything else before it runs; here it is shown as String() writes it.
function requestFor(operation: Operation, binding: Binding, agent: string): ApprovalRequest {
  const { kind, args } = operation;
  const chat = { agent, root: binding.root, calling: operation.callingSessionId, folder: binding.folder };
  const text = (name: string) => String(args[name] ?? "");
  if (kind === "write" || kind === "delete") {
    const bytes = kind === "write" ? Buffer.byteLength(text("data"), "base64") : null;
    return { kind: "change", chat, action: kind, path: text("key"), bytes };
  }
  if (kind === "write_stdin") return { kind: "input", chat, process: text("session_id"), data: text("data") };
  const workdir = args.workdir == null ? null : String(args.workdir);
  return { kind: "command", chat, command: text("command"), workdir, background: kind === "start" };
}

// Settles when *promise* does, or at once when *signal* aborts.
function settled(promise: Promise<void>, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      signal.removeEventListener("abort", done);
      resolve();
    };
    signal.addEventListener("abort", done, { once: true });
    void promise.then(done);
  });
}

export class Approvals {
  // Each chat's last operation in line: one prompt per chat at a time, in the order its operations came.
  private readonly lines = new Map<string, Promise<void>>();

  constructor(private readonly options: ApprovalsOptions) {}

  /**
   * Null lets the operation run; an outcome is its denial. Settles once *signal*
   * aborts (a cancel, a suspend): an operation waiting its turn leaves the line,
   * and its open prompt is dismissed.
   */
  async admit(operation: Operation, signal: AbortSignal): Promise<Outcome | null> {
    // Fail closed: the tool hosts read the binding again only when it runs, so a bind
    // arriving meanwhile must not let it run unasked.
    if (ASKED.has(operation.kind) && !this.options.bindings.get(operation.sessionId)) return FOLDER_UNAVAILABLE;
    if (!this.asking(operation)) return null;
    const root = operation.sessionId;
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
      // A "Stop asking" while it waited its turn lets it through unasked.
      const binding = signal.aborted ? undefined : this.asking(operation);
      if (!binding) return null;
      const request = requestFor(operation, binding, this.options.agent);
      let answer: ApprovalAnswer;
      try {
        answer = await this.options.prompts.approve(request, signal);
      } catch (error) {
        const why = error instanceof Error ? error.message : String(error);
        return denied(request, `This computer could not ask its user about this: ${why}`);
      }
      // A dismissed prompt's answer is not its user's.
      if (signal.aborted) return null;
      if (answer === "stop_asking") this.options.bindings.setMode(root, "free");
      return answer === "allow" || answer === "stop_asking" ? null : denied(request, DENIED[request.kind]);
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

  /** Work freely, once the user confirms it in the desktop's own window. True when the chat now works freely. */
  async requestFreeMode(sessionId: string, signal: AbortSignal): Promise<boolean> {
    const binding = this.bound(sessionId);
    if (binding.mode === "free") return true;
    const chat = { agent: this.options.agent, root: sessionId, calling: sessionId, folder: binding.folder };
    const confirmed = await this.options.prompts.confirmFreeMode(chat, signal);
    if (confirmed !== true || signal.aborted) return false;
    this.options.bindings.setMode(sessionId, "free");
    return true;
  }

  // The chat's binding, when this operation must be asked about now.
  private asking(operation: Operation): Binding | undefined {
    if (!ASKED.has(operation.kind)) return undefined;
    const binding = this.options.bindings.get(operation.sessionId);
    if (binding?.mode !== "ask") return undefined;
    // The file helper takes only its own resolved paths as keys, so a link or a ".."
    // cannot carry a write that skips its prompt here out of this folder.
    const key = operation.args.key;
    if (operation.kind === "write" && typeof key === "string" && key.startsWith(`${binding.folder}/${RESULTS}/`)) {
      return undefined;
    }
    return binding;
  }

  private bound(sessionId: string): Binding {
    const binding = this.options.bindings.get(sessionId);
    if (!binding) throw new Error("This chat has no folder on this computer");
    return binding;
  }
}

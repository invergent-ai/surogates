// The Executor behind the binder once commands run in the VM (spec, Section 11,
// "The VmExecutor"). The file kinds go to the root's file host, and its helper in
// srt; the process kinds go to the guest, while the root's file host holds the
// folder: its lock, and, around a command, the hook guard's refusal and its look after.
// A project thread's root works in its copy of the folder (spec, Section 13): the
// executor's copies have the guest make it, its file host holds it, and the guest shares
// it at the folder's path.

import type { FolderGuards } from "../binding/folder.js";
import { Copies, type Making } from "../history/copies.js";
import { type Guard, ToolHosts, type ToolHostsOptions } from "../hosts/tool-hosts.js";
import type { Operation, Outcome } from "../link/protocol.js";
import type { Executor } from "../operations/runner.js";
import type { VmClient } from "./client.js";

export const PROCESS_KINDS: ReadonlySet<string> = new Set(["run", "which", "start", "poll", "read_output", "wait", "kill", "write_stdin", "list_processes"]);

// The hook guard around the kinds that run commands (spec, Section 11, "The VmExecutor"): a run
// is refused while it refuses, and looked after; a start, and input to a process, are refused.
// A live process is looked after every 5 s by the file host. The other kinds run no command.
const GUARDS: Partial<Record<string, Guard>> = { run: "around", start: "before", write_stdin: "before" };

export interface VmExecutorOptions extends Omit<ToolHostsOptions, "copies"> {
  vm: Pick<VmClient, "perform" | "teardown" | "onProcesses" | "onAsk" | "history" | "unplace">;
  // Who this device's threads were started by: a folder's first commit, and the pickups of its edits, are theirs.
  user: string;
  // Told as a thread's copy is being made, and as that ends: a first copy of a large folder takes minutes.
  making?(event: Making): void;
}

export class VmExecutor implements Executor {
  private readonly files: ToolHosts;
  // The copies its threads work in: made by git in the guest, in the folder's place in the app's data.
  private readonly copies: Copies;
  private readonly unheard: () => void;
  private readonly unasked: () => void;

  constructor(private readonly options: VmExecutorOptions) {
    // A copy that is its thread's no more: the hosts on it go before the thread's next operation.
    this.copies = new Copies({
      dataDir: options.dataDir, user: options.user, vm: options.vm, replaced: (root) => this.files.replaced(root), making: (event) => options.making?.(event),
    });
    // The guest's root goes before its file host lets the folder go: what its
    // commands left running must not outlive the folder's lock and its hook guard.
    this.files = new ToolHosts({ ...options, copies: this.copies, release: (root) => options.vm.teardown(root) });
    // The handles of its roots' processes in the guest go to each root's file host, which keeps them.
    this.unheard = options.vm.onProcesses((root, change) => this.files.processes(root, change));
    // A destination off the package hosts that one of its chats' commands asked for: its approvals
    // decide, as long as something of the chat runs. Another device's chat is not its to answer.
    this.unasked = options.vm.onAsk((root, asked) => (options.bindingOf(root) ? this.files.ask(root, asked) : null));
  }

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (!PROCESS_KINDS.has(operation.kind)) return this.files.run(operation, signal);
    // What the root's host holds is what the guest shares for it: its folder, or a thread's copy at the folder's path.
    return this.files.guarded(operation, signal, GUARDS[operation.kind] ?? null, ({ folder, at }, aborted, ended) => this.options.vm.perform({
      id: operation.id, root: operation.sessionId, folder, ...(at === undefined ? {} : { at }), kind: operation.kind, args: operation.args, ended,
    }, aborted));
  }

  guards(): FolderGuards {
    return this.files.guards();
  }

  // Its threads each work in a copy of their folder, which its copies make.
  keepsCopies(): boolean {
    return this.files.keepsCopies();
  }

  // The chats with a background process alive in the guest: the quit counts them as working.
  live(): string[] {
    return this.files.liveRoots();
  }

  stop(): Promise<void> {
    this.unheard();
    this.unasked();
    // No place is let go later, and a copy's making is told to stop: the hosts on the copies stop next.
    this.copies.stop();
    return this.files.stop();
  }

  end(): Promise<void> {
    return this.files.end();
  }
}

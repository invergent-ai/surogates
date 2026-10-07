// The Executor behind the binder once commands run in the VM (spec, Section 11,
// "The VmExecutor"). The file kinds go to the root's file host, and its helper in
// srt; the process kinds go to the guest, while the root's file host holds the
// folder: its lock, and, around a command, the hook guard's refusal and its look after.

import type { FolderGuards } from "../binding/folder.js";
import { type Guard, ToolHosts, type ToolHostsOptions } from "../hosts/tool-hosts.js";
import type { Operation, Outcome } from "../link/protocol.js";
import type { Executor } from "../operations/runner.js";
import type { VmClient } from "./client.js";

const PROCESS_KINDS = new Set(["run", "which", "start", "poll", "read_output", "wait", "kill", "write_stdin", "list_processes"]);

// The hook guard around the kinds that run commands (spec, Section 11, "The VmExecutor"): a run
// is refused while it refuses, and looked after; a start, and input to a process, are refused.
// A live process is looked after every 5 s by the file host. The other kinds run no command.
const GUARDS: Partial<Record<string, Guard>> = { run: "around", start: "before", write_stdin: "before" };

export interface VmExecutorOptions extends ToolHostsOptions {
  vm: Pick<VmClient, "perform" | "teardown" | "onProcesses" | "onAsk">;
}

export class VmExecutor implements Executor {
  private readonly files: ToolHosts;
  private readonly unheard: () => void;
  private readonly unasked: () => void;

  constructor(private readonly options: VmExecutorOptions) {
    // The guest's root goes before its file host lets the folder go: what its
    // commands left running must not outlive the folder's lock and its hook guard.
    this.files = new ToolHosts({ ...options, release: (root) => options.vm.teardown(root) });
    // The handles of its roots' processes in the guest go to each root's file host, which keeps them.
    this.unheard = options.vm.onProcesses((root, change) => this.files.processes(root, change));
    // A destination off the package hosts that one of its chats' commands asked for: its approvals
    // decide, as long as something of the chat runs. Another device's chat is not its to answer.
    this.unasked = options.vm.onAsk((root, asked) => (options.bindingOf(root) ? this.files.ask(root, asked) : null));
  }

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (!PROCESS_KINDS.has(operation.kind)) return this.files.run(operation, signal);
    return this.files.guarded(operation, signal, GUARDS[operation.kind] ?? null, ({ folder, dev, ino, boot }, aborted, ended) => this.options.vm.perform({
      id: operation.id, root: operation.sessionId, folder: { path: folder, dev, ino, boot }, kind: operation.kind, args: operation.args, ended,
    }, aborted));
  }

  guards(): FolderGuards {
    return this.files.guards();
  }

  // The chats with a background process alive in the guest: the quit counts them as working.
  live(): string[] {
    return this.files.liveRoots();
  }

  stop(): Promise<void> {
    this.unheard();
    this.unasked();
    return this.files.stop();
  }

  end(): Promise<void> {
    return this.files.end();
  }
}

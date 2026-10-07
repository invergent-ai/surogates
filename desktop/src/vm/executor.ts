// The Executor behind the binder once commands run in the VM (spec, Section 11,
// "The VmExecutor"). The file kinds go to the root's file host, and its helper in
// srt; the process kinds go to the guest, while the root's file host holds the
// folder: its lock, and, around a command, the hook guard's refusal and its look after.
// A command runs once the folder's protected keys, as the file host names them, are
// read-only in its root's namespace.

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
  vm: Pick<VmClient, "perform" | "teardown" | "onProcesses" | "protect">;
}

export class VmExecutor implements Executor {
  private readonly files: ToolHosts;
  private readonly unheard: () => void;

  constructor(private readonly options: VmExecutorOptions) {
    // The guest's root goes before its file host lets the folder go: what its
    // commands left running must not outlive the folder's lock and its hook guard.
    // The folder's protected keys its file host finds between commands are bound in the guest as found.
    this.files = new ToolHosts({ ...options, release: (root) => options.vm.teardown(root), protect: (root, keys) => options.vm.protect(root, keys) });
    // The handles of its roots' processes in the guest go to each root's file host, which keeps them.
    this.unheard = options.vm.onProcesses((root, change) => this.files.processes(root, change));
  }

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (!PROCESS_KINDS.has(operation.kind)) return this.files.run(operation, signal);
    const guard = GUARDS[operation.kind] ?? null;
    return this.files.guarded(operation, signal, guard, ({ folder, dev, ino, boot }, aborted, ended, keys) => this.options.vm.perform({
      id: operation.id, root: operation.sessionId, folder: { path: folder, dev, ino, boot }, kind: operation.kind, args: operation.args, ended,
      // What runs a command runs it once the folder's protected keys are read-only.
      ...(guard ? { protect: keys } : {}),
    }, aborted));
  }

  guards(): FolderGuards {
    return this.files.guards();
  }

  stop(): Promise<void> {
    this.unheard();
    return this.files.stop();
  }

  end(): Promise<void> {
    return this.files.end();
  }
}

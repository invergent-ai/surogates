// The Executor behind the binder once commands run in the VM (spec, Section 11,
// "The VmExecutor"). The file kinds go to the root's file host, and its helper in
// srt; the process kinds go to the guest, while the root's file host holds the
// folder: its lock, and, around a command, the hook guard's refusal and its look after.

import type { FolderGuards } from "../binding/folder.js";
import { ToolHosts, type ToolHostsOptions } from "../hosts/tool-hosts.js";
import type { Operation, Outcome } from "../link/protocol.js";
import type { Executor } from "../operations/runner.js";
import type { VmClient } from "./client.js";

const PROCESS_KINDS = new Set(["run", "which", "start", "poll", "read_output", "wait", "kill", "write_stdin", "list_processes"]);

export interface VmExecutorOptions extends ToolHostsOptions {
  vm: Pick<VmClient, "perform" | "teardown" | "onProcesses">;
}

export class VmExecutor implements Executor {
  private readonly files: ToolHosts;
  private readonly unheard: () => void;

  constructor(private readonly options: VmExecutorOptions) {
    // The guest's root goes before its file host lets the folder go: what its
    // commands left running must not outlive the folder's lock and its hook guard.
    this.files = new ToolHosts({ ...options, release: (root) => options.vm.teardown(root) });
    // The handles of its roots' processes in the guest go to each root's file host, which keeps them.
    this.unheard = options.vm.onProcesses((root, change) => this.files.processes(root, change));
  }

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (!PROCESS_KINDS.has(operation.kind)) return this.files.run(operation, signal);
    return this.files.guarded(operation, signal, operation.kind === "run", ({ folder, dev, ino, boot }, aborted, ended) => this.options.vm.perform({
      id: operation.id, root: operation.sessionId, folder: { path: folder, dev, ino, boot }, kind: operation.kind, args: operation.args, ended,
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

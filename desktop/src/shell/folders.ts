// Settings → Folders and permissions (spec, Section 4): each folder of this computer its chats
// work on, in the order a chat first worked on it, and each chat on it, by its title, in its mode,
// with the hosts its user let it reach past the package hosts, and its background processes alive in the VM.

import { randomUUID } from "node:crypto";

import type { ProcessHandle } from "../guest/processes.js";
import type { Bindings, Mode } from "../journal/bindings.js";
import type { Operation } from "../link/protocol.js";
import type { ProcessesChange } from "../vm/manager.js";

export interface ChatRow {
  root: string;
  title: string; // the agent's words, shown as text
  mode: Mode;
  hosts: string[]; // in the order allowed, each on every port
  processes: Array<{ id: string; command: string }>; // the agent's words, shown as text
}

export interface FolderRow {
  folder: string;
  chats: ChatRow[];
}

/** Each chat's background processes alive in the VM, as the VM tells each change of them (VmClient.onProcesses). */
export class LiveProcesses {
  private readonly byRoot = new Map<string, ProcessHandle[]>();

  heard(root: string, change: ProcessesChange): void {
    // Gone: its guest went, and its processes with it.
    if ("gone" in change) this.byRoot.delete(root);
    else this.byRoot.set(root, change.handles.filter((handle) => handle.ended === undefined));
  }

  clear(): void {
    this.byRoot.clear();
  }

  of(root: string): Array<{ id: string; command: string }> {
    return (this.byRoot.get(root) ?? []).map(({ id, command }) => ({ id, command }));
  }
}

/** What stops chat *root*'s background process *id*, as the agent's own kill would: its user's, from Settings. */
export function stopOperation(root: string, id: string): Operation {
  return {
    id: `settings-${randomUUID()}`, sessionId: root, callingSessionId: root, invocationId: "settings", ordinal: 0,
    kind: "kill", args: { session_id: id }, digest: "",
  };
}

/** The folders and their chats, from *bindings*; *title* names each chat, and *processes* tells what each runs. */
export async function listFolders(
  bindings: Pick<Bindings, "all" | "domains">,
  title: (root: string) => Promise<string>,
  processes: Pick<LiveProcesses, "of">,
): Promise<FolderRow[]> {
  const all = bindings.all();
  const titles = await Promise.all(all.map((binding) => title(binding.root)));
  const folders = new Map<string, ChatRow[]>();
  all.forEach((binding, at) => {
    const chats = folders.get(binding.folder) ?? [];
    // A mode it does not know asks, as the approvals take it.
    chats.push({
      root: binding.root, title: titles[at]!, mode: binding.mode === "free" ? "free" : "ask",
      hosts: bindings.domains(binding.root), processes: processes.of(binding.root),
    });
    folders.set(binding.folder, chats);
  });
  return [...folders].map(([folder, chats]) => ({ folder, chats }));
}

// Settings → Folders and permissions (spec, Section 4): each folder of this computer its chats
// work on, in the order a chat first worked on it, and each chat on it, by its title, in its mode,
// with the hosts its user let it reach past the package hosts.

import type { Bindings, Mode } from "../journal/bindings.js";

export interface ChatRow {
  root: string;
  title: string; // the agent's words, shown as text
  mode: Mode;
  hosts: string[]; // in the order allowed, each on every port
}

export interface FolderRow {
  folder: string;
  chats: ChatRow[];
}

/** The folders and their chats, from *bindings*; *title* names each chat. */
export async function listFolders(bindings: Pick<Bindings, "all" | "domains">, title: (root: string) => Promise<string>): Promise<FolderRow[]> {
  const all = bindings.all();
  const titles = await Promise.all(all.map((binding) => title(binding.root)));
  const folders = new Map<string, ChatRow[]>();
  all.forEach((binding, at) => {
    const chats = folders.get(binding.folder) ?? [];
    // A mode it does not know asks, as the approvals take it.
    chats.push({ root: binding.root, title: titles[at]!, mode: binding.mode === "free" ? "free" : "ask", hosts: bindings.domains(binding.root) });
    folders.set(binding.folder, chats);
  });
  return [...folders].map(([folder, chats]) => ({ folder, chats }));
}

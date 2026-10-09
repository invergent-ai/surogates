// What the file panel says and holds for each chat, by the chat's id. It outlives the panel: a
// change still under way as the panel folds away, or shows another chat, ends in its own chat's
// entry, and shows only once the panel shows that chat again.
import { useSyncExternalStore } from "react";

import type { AgentChatWorkspaceEntry } from "../../types";

export interface ChatFiles {
  // The tree, kept only while the panel shows the chat: it is read again on the way back.
  entries?: AgentChatWorkspaceEntry[];
  // The tree stopped short of the whole folder: at its caps, or a computer out of handles.
  truncated?: boolean;
  loading?: boolean;
  error?: string | null;
  // What the tree's read waits for while it does, as its computer being back online: the host says it.
  waiting?: string | null;
  // What the last change says: its wait, its end or its refusal.
  notice?: string | null;
  uploading?: boolean;
  // The file a delete is asked for, and whether it is under way.
  deleteTarget?: string | null;
  deleting?: boolean;
}

const NONE: ChatFiles = {};
// ponytail: one entry per chat whose files were shown, until the account signs out: a few flags and
// a line each, with no tree once the panel leaves it. Bound the map if a page ever shows thousands.
const chats = new Map<string, ChatFiles>();
const listeners = new Set<() => void>();

function changed(): void {
  for (const listener of listeners) listener();
}

export function changeChatFiles(chat: string, changes: ChatFiles): void {
  chats.set(chat, { ...chats.get(chat), ...changes });
  changed();
}

/**
 * The panel leaves *chat*, folded away or moved on: its tree goes, and so does a delete it asked and
 * the user left unanswered; one under way keeps its dialog to its end.
 */
export function leaveChatFiles(chat: string): void {
  const was = chats.get(chat);
  if (!was) return;
  const { entries: _tree, truncated: _cut, ...kept } = was;
  chats.set(chat, was.deleting ? kept : { ...kept, deleteTarget: null });
  changed();
}

/** Every chat's entry gone, as the account signs out, and as a test starts. */
export function forgetChatFiles(): void {
  chats.clear();
  changed();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useChatFiles(chat: string | null): ChatFiles {
  return useSyncExternalStore(subscribe, () => (chat === null ? NONE : chats.get(chat) ?? NONE));
}

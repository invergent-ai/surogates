// What the file panel says and holds for each chat, by the chat's id. It outlives the panel: a
// change still under way as the panel folds away, or shows another chat, ends in its own chat's
// entry, and shows only once the panel shows that chat again.
import { useSyncExternalStore } from "react";

import type { AgentChatWorkspaceEntry } from "../../types";

export interface ChatFiles {
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
// ponytail: kept for the page's life, one small entry per chat whose files were shown; forget an
// account's on sign-out if that ever grows.
const chats = new Map<string, ChatFiles>();
const listeners = new Set<() => void>();

export function changeChatFiles(chat: string, changes: ChatFiles): void {
  chats.set(chat, { ...chats.get(chat), ...changes });
  for (const listener of listeners) listener();
}

/** Every chat's entry gone: a test starts from none. */
export function forgetChatFiles(): void {
  chats.clear();
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

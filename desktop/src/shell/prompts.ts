// The desktop's own prompts (spec, Sections 4 and 8). The folder dialog is the system's
// (dialog.showOpenDialog with openDirectory and createDirectory); the folder sheet, the
// approval prompts, the Work-freely confirmation and the hand back's are prompt windows of
// the app's own. Each takes its turn over the app's window, one at a time.

import { type BrowserWindow, dialog } from "electron";

import type { ApprovalAnswer, ApprovalPrompts } from "../binding/approvals.js";
import type { FolderPrompts } from "../binding/binder.js";
import { guestPathOf, hostPathOf } from "../session-path.js";
import { approval, folderSheet, freeMode, handBack, type HandBackRequest, type PromptContent } from "./prompt-content.js";
import { PromptQueue, TIMEOUT } from "./prompt-queue.js";
import { openPrompt, type PromptAnswer } from "./prompt-window.js";

export interface BrowserPrompts {
  // The desktop's own confirmation before the agent drives its browser again. Only true hands it back:
  // Keep control, Escape, a window closed some other way, its asker's page gone, and nobody in time, all keep it.
  confirmHandBack(request: HandBackRequest, signal: AbortSignal): Promise<boolean>;
}

export interface DesktopPromptsOptions {
  parent(): BrowserWindow | undefined; // the app's window
  page: string; // prompt.html
  preload: string;
  unseen(): void; // a prompt waits for the hidden window to be shown
}

// The dialog opens at, and answers, this computer's own path of a folder; the binder names each by the session's path
// (session-path.ts). A folder the session has no path for, as one on no drive, is answered as the dialog gave it,
// which the binder's check refuses; the dialog opens where that one was.
const ownPath = (folder: string): string => {
  try {
    return hostPathOf(folder);
  } catch {
    return folder;
  }
};

async function pick(parent: BrowserWindow | undefined, startIn: string): Promise<string | null> {
  const options = {
    title: "Choose a folder",
    buttonLabel: "Choose",
    defaultPath: ownPath(startIn),
    properties: ["openDirectory", "createDirectory"] as Array<"openDirectory" | "createDirectory">,
  };
  const { canceled, filePaths } = parent ? await dialog.showOpenDialog(parent, options) : await dialog.showOpenDialog(options);
  const picked = canceled ? null : (filePaths[0] ?? null);
  if (picked === null) return null;
  try {
    return guestPathOf(picked);
  } catch {
    return picked;
  }
}

// ponytail: one line for the app's one window; one per window once there is a second.
export function desktopPrompts(options: DesktopPromptsOptions): FolderPrompts & ApprovalPrompts & BrowserPrompts {
  const queue = new PromptQueue();
  // The button pressed, TIMEOUT, or null: closed, or dismissed.
  const ask = (content: PromptContent, signal: AbortSignal): Promise<PromptAnswer | typeof TIMEOUT | null> =>
    queue.ask((shown) => {
      const parent = options.parent();
      if (!parent) throw new Error("Surogate has no window to ask in");
      return openPrompt({ parent, page: options.page, preload: options.preload, content, queue, unseen: options.unseen }, shown);
    }, signal);
  return {
    // ponytail: the system's dialog cannot be closed from here, so a dialog left open holds the line until the user closes it.
    pickFolder: async (startIn, signal) => {
      const picked = await queue.ask(() => pick(options.parent(), startIn), signal);
      return picked === TIMEOUT ? null : picked;
    },
    async confirmFolder(sheet, signal) {
      const answer = await ask(folderSheet(sheet), signal);
      if (answer === null || answer === TIMEOUT) return null;
      if (answer.button === "change") return "change";
      return answer.button === "accept" && (answer.choice === "free" || answer.choice === "ask") ? { mode: answer.choice } : null;
    },
    // Its buttons' ids are the answers; one closed some other way, or dismissed, denies.
    async approve(request, signal) {
      const answer = await ask(approval(request), signal);
      if (answer === TIMEOUT) return "timeout";
      return answer === null ? "deny" : (answer.button as ApprovalAnswer);
    },
    async confirmFreeMode(chat, signal) {
      const answer = await ask(freeMode(chat), signal);
      return answer !== null && answer !== TIMEOUT && answer.button === "free";
    },
    async confirmHandBack(request, signal) {
      const answer = await ask(handBack(request), signal);
      return answer !== null && answer !== TIMEOUT && answer.button === "hand_back";
    },
  };
}

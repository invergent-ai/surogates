// The desktop's own prompts (spec, Sections 4 and 8). The folder dialog is the system's,
// as Claude Desktop picks a folder (dialog.showOpenDialog with openDirectory and
// createDirectory); the folder sheet, the approval prompts and the Work-freely confirmation
// are prompt windows of the app's own. Each takes its turn over the app's window, one at a time.

import { type BrowserWindow, dialog } from "electron";

import type { ApprovalAnswer, ApprovalPrompts } from "../binding/approvals.js";
import type { FolderPrompts } from "../binding/binder.js";
import { approval, folderSheet, freeMode, type PromptContent } from "./prompt-content.js";
import { PromptQueue, TIMEOUT } from "./prompt-queue.js";
import { openPrompt, type PromptAnswer } from "./prompt-window.js";

export interface DesktopPromptsOptions {
  parent(): BrowserWindow | undefined; // the app's window
  page: string; // prompt.html
  preload: string;
  unseen(): void; // a prompt waits for the hidden window to be shown
}

async function pick(parent: BrowserWindow | undefined, startIn: string): Promise<string | null> {
  const options = {
    title: "Choose a folder",
    buttonLabel: "Choose",
    defaultPath: startIn,
    properties: ["openDirectory", "createDirectory"] as Array<"openDirectory" | "createDirectory">,
  };
  const { canceled, filePaths } = parent ? await dialog.showOpenDialog(parent, options) : await dialog.showOpenDialog(options);
  return canceled ? null : (filePaths[0] ?? null);
}

// ponytail: one line for the app's one window; one per window once there is a second.
export function desktopPrompts(options: DesktopPromptsOptions): FolderPrompts & ApprovalPrompts {
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
  };
}

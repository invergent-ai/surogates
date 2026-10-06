// The desktop's prompts until its own folder sheet and approval prompts are built. The
// folder is picked in the system's folder dialog, as Claude Desktop picks one
// (dialog.showOpenDialog with openDirectory and createDirectory), and confirmed in a
// native message box. No approval prompt allows anything, and no chat works freely.

import { basename } from "node:path";

import { type BrowserWindow, dialog } from "electron";

import type { ApprovalPrompts } from "../binding/approvals.js";
import type { FolderPrompts, FolderSheet } from "../binding/binder.js";

export function folderPrompts(parent: () => BrowserWindow | undefined): FolderPrompts {
  return {
    async pickFolder(startIn) {
      const options = {
        title: "Choose a folder for this chat",
        buttonLabel: "Choose",
        defaultPath: startIn,
        properties: ["openDirectory", "createDirectory"] as Array<"openDirectory" | "createDirectory">,
      };
      const window = parent();
      const { canceled, filePaths } = window ? await dialog.showOpenDialog(window, options) : await dialog.showOpenDialog(options);
      return canceled ? null : (filePaths[0] ?? null);
    },
    async confirmFolder(sheet: FolderSheet, signal) {
      const linked = sheet.links
        ? ` ${sheet.links.complete ? "" : "At least "}${sheet.links.count} of its files are also linked from outside it.`
        : "";
      const buttons = sheet.refusal ? ["Change…", "Cancel"] : ["Use this folder", "Change…", "Cancel"];
      const options = {
        type: sheet.refusal ? ("warning" as const) : ("question" as const),
        message: sheet.refusal ? `${basename(sheet.folder)} cannot be used` : `Work in ${basename(sheet.folder)}?`,
        detail: sheet.refusal ?? `${sheet.agent} will read and change the files in ${sheet.folder}, and run commands there.${linked}`,
        buttons,
        defaultId: 0,
        cancelId: buttons.length - 1,
        noLink: true,
        signal,
      };
      const window = parent();
      const { response } = window ? await dialog.showMessageBox(window, options) : await dialog.showMessageBox(options);
      const answer = buttons[response];
      if (answer === "Use this folder") return { mode: sheet.mode };
      return answer === "Change…" ? "change" : null;
    },
  };
}

export const refusingApprovals: ApprovalPrompts = {
  approve: () => Promise.resolve("deny"),
  confirmFreeMode: () => Promise.resolve(false),
};

// What the desktop's prompts show (spec, Sections 4 and 8). The main process writes
// every word; what the agent sent, and the names of the user's folders and files, go
// in their own fields, which the page sets as text and never as markup.

import { basename } from "node:path";

import type { FolderSheet } from "../binding/binder.js";
import type { LinkSummary } from "../binding/links.js";

export interface PromptButton {
  id: string; // what the answer names
  label: string;
  // It gives the agent more than it had: held back until the input protection has passed.
  allows: boolean;
}

// One thing asked about, shown whole: a folder, a command, a path, a preview.
export interface PromptDetail {
  label: string;
  value: string;
  // In a monospaced block that scrolls: a command, or a path.
  code: boolean;
  // The special characters shown as themselves: a command's newlines and tabs. Every other one shows as its code point.
  keep: string;
}

// The sheet's mode: one of its options, chosen as the prompt opens.
export interface PromptChoice {
  legend: string;
  options: Array<{ value: string; label: string; description: string }>;
  value: string;
}

export interface PromptContent {
  title: string;
  lead: string;
  details: PromptDetail[];
  notes: string[]; // what the user should know before answering
  choice: PromptChoice | null;
  buttons: PromptButton[]; // left to right; the last is drawn as the main one
  focus: string; // the button focused as it opens
  cancel: string; // Escape's, and a window closed some other way
  enter: string | null; // what Enter does from anywhere but a button
  height: number; // the window's, in px
}

const button = (id: string, label: string, allows = false): PromptButton => ({ id, label, allows });
const code = (label: string, value: string, keep = ""): PromptDetail => ({ label, value, code: true, keep });
const named = (path: string) => basename(path) || path;

// The two modes (spec, Section 4), as the sheet offers them.
const MODES: Omit<PromptChoice, "value"> = {
  legend: "How it works in this folder",
  options: [
    { value: "free", label: "Work freely", description: "Runs commands and changes files without asking first." },
    { value: "ask", label: "Ask every time", description: "Asks you before each command, each file change and each input to a running command." },
  ],
};

function linked({ count, examples, complete }: LinkSummary): string {
  if (count === 0) return "Surogate could not look through all of this folder for files that are also linked from elsewhere.";
  const files = `${count} ${count === 1 ? "file in this folder is" : "files in this folder are"}`;
  const more = count > examples.length ? ", and others" : "";
  return `${complete ? "" : "At least "}${files} also linked from elsewhere; commands the agent runs can change those copies too: ${examples.join(", ")}${more}.`;
}

/** The folder sheet: the folder, its mode, and what the user should know; or why it cannot be used. */
export function folderSheet(sheet: FolderSheet): PromptContent {
  const name = named(sheet.folder);
  const folder = code("Folder", sheet.folder);
  if (sheet.refusal !== null) {
    return {
      title: `${name} cannot be used`,
      lead: `${sheet.agent} cannot work in this folder: ${sheet.refusal}. Choose another folder.`,
      details: [folder],
      notes: [],
      choice: null,
      buttons: [button("cancel", "Cancel"), button("change", "Change…")],
      focus: "change",
      cancel: "cancel",
      enter: null,
      height: 280,
    };
  }
  return {
    title: `Work in ${name}?`,
    lead: `${sheet.agent} will read and change the files in this folder, and run commands there. It works in the cloud: the files it reads and what its commands print are sent to it, and can stay in the conversation.`,
    details: [folder],
    notes: sheet.links ? [linked(sheet.links)] : [],
    choice: { ...MODES, value: sheet.mode },
    buttons: [button("cancel", "Cancel"), button("change", "Change…"), button("accept", "Use this folder", true)],
    focus: "accept",
    cancel: "cancel",
    enter: "accept",
    height: sheet.links ? 550 : 490,
  };
}

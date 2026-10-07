// What the desktop's prompts show (spec, Sections 4 and 8). The main process writes
// every word; what the agent sent, and the names of the user's folders and files, go
// in their own fields, which the page sets as text and never as markup.

import { basename, relative } from "node:path";

import { type ApprovalRequest, type ChatLabel, PREVIEW_BYTES } from "../binding/approvals.js";
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

// Who asks: the chat's agent, or one of its sub-agents.
const asker = (chat: ChatLabel) => (chat.calling === chat.root ? chat.agent : `A sub-agent of ${chat.agent}`);

// A path in the chat's folder, from the folder; any other whole.
const inFolder = (folder: string, path: string) => (path.startsWith(`${folder}/`) ? relative(folder, path) : path);

export function sizeOf(bytes: number): string {
  if (bytes < 1024) return `${bytes} ${bytes === 1 ? "byte" : "bytes"}`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 102.4) / 10} KB`;
  return `${Math.round(bytes / (1024 * 102.4)) / 10} MB`;
}

// What an operation's prompt offers: Deny first, focused, and what Escape and a closed window answer.
const OPERATION = {
  notes: [],
  choice: null,
  buttons: [button("deny", "Deny"), button("stop_asking", "Allow and stop asking", true), button("allow", "Allow once", true)],
  focus: "deny",
  cancel: "deny",
  enter: null,
};

/** The prompt for one operation or network destination (spec, Section 4): its buttons' ids are the answers. */
export function approval(request: ApprovalRequest): PromptContent {
  const { chat } = request;
  const folder = named(chat.folder);
  if (request.kind === "command") {
    return {
      ...OPERATION,
      title: request.background ? `Start a background command in ${folder}?` : `Run a command in ${folder}?`,
      lead: `${asker(chat)} wants to run this on this computer.`,
      details: [code("Command", request.command, "\n\t"), ...(request.workdir === null ? [] : [code("In", request.workdir)])],
      height: 380,
    };
  }
  if (request.kind === "change") {
    const file = code("File", inFolder(chat.folder, request.path));
    // A path outside the folder is named whole, and its file host refuses it.
    const where = request.path.startsWith(`${chat.folder}/`) ? ` in ${folder}` : "";
    if (request.action === "delete") {
      return { ...OPERATION, title: `Delete ${named(request.path)}?`, lead: `${asker(chat)} wants to delete this file${where}.`, details: [file], height: 300 };
    }
    const bytes = request.bytes ?? 0;
    const { preview } = request;
    const content: PromptDetail = preview === null
      ? { label: `New content, ${sizeOf(bytes)}`, value: "Not text.", code: false, keep: "" }
      : code(preview.cut ? `The first ${sizeOf(PREVIEW_BYTES)} of ${sizeOf(bytes)}` : `New content, ${sizeOf(bytes)}`, preview.text, "\n\t");
    return {
      ...OPERATION, title: `Write ${named(request.path)}?`, lead: `${asker(chat)} wants to write this file${where}.`, details: [file, content], height: 420,
    };
  }
  if (request.kind === "input") {
    const command: PromptDetail = request.command === null
      ? { label: "To the command", value: `A command Surogate did not start in this session (${request.process}).`, code: false, keep: "" }
      : code("To the command", request.command, "\n\t");
    return {
      ...OPERATION,
      title: "Type into a running command?",
      lead: `${asker(chat)} wants to send this input to a command running in ${folder}.`,
      details: [command, code("Input", request.data)],
      height: 380,
    };
  }
  return {
    title: `Connect to ${request.host}?`,
    lead: `A command in ${folder} wants to connect to ${request.host} on port ${request.port}. Allow lets through the connections waiting now; later ones ask again.`,
    details: [code("Address", `${request.host}:${request.port}`)],
    notes: request.privateNetwork ? ["This address is on a private network, such as a home or office network, or a VPN."] : [],
    choice: null,
    buttons: [button("deny", "Deny"), button("allow_session", "Allow all its ports for this chat", true), button("allow", "Allow", true)],
    focus: "deny",
    cancel: "deny",
    enter: null,
    height: request.privateNetwork ? 340 : 290,
  };
}

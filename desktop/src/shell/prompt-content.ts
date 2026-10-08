// What the desktop's prompts show (spec, Sections 4 and 8). The main process writes
// every word; what the agent sent, and the names of the user's folders and files, go
// in their own fields, which the page sets as text and never as markup.

import { basename, posix } from "node:path";

import { type ApprovalRequest, type BrowserAction, type ChatLabel, PREVIEW_BYTES } from "../binding/approvals.js";
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
  focus: string; // the button focused as it opens, or "choice": the option chosen
  cancel: string; // Escape's, and a window closed some other way
  enter: string | null; // what Enter does from anywhere but a button
  height: number; // the window's, in px
}

const button = (id: string, label: string, allows = false): PromptButton => ({ id, label, allows });
const code = (label: string, value: string, keep = ""): PromptDetail => ({ label, value, code: true, keep });
const named = (path: string) => basename(path) || path;
// A command's label, with how many lines it has once it has more than one: what follows its first line can be out of view.
const lined = (label: string, text: string) => {
  const lines = text.split("\n").length;
  return lines > 1 ? `${label}, ${lines.toLocaleString("en")} lines` : label;
};

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
    // On the mode, not on Use this folder: a space typed as the sheet opens only picks the mode already picked.
    focus: "choice",
    cancel: "cancel",
    enter: "accept",
    height: sheet.links ? 550 : 490,
  };
}

// Who asks: the chat's agent, or one of its sub-agents.
const asker = (chat: ChatLabel) => (chat.calling === chat.root ? chat.agent : `A sub-agent of ${chat.agent}`);

// Whether *path* plainly names something in the chat's folder: one with a "..", a "." or a doubled slash is
// not taken as the folder's, though it may lead there; its file host refuses it.
const inside = (folder: string, path: string) =>
  path.startsWith(`${folder}/`) && path.length > folder.length + 1 && posix.normalize(path) === path;

// A path in the chat's folder, as sent, from the folder; any other whole.
const inFolder = (folder: string, path: string) => (inside(folder, path) ? path.slice(folder.length + 1) : path);

export function sizeOf(bytes: number): string {
  if (bytes < 1024) return `${bytes} ${bytes === 1 ? "byte" : "bytes"}`;
  // Rounded to a tenth: what rounds to 1024 KB is 1 MB.
  const kb = Math.round(bytes / 102.4) / 10;
  if (kb < 1024) return `${kb} KB`;
  return `${Math.round(bytes / (1024 * 102.4)) / 10} MB`;
}

// The most of a host a title shows: a longer one is cut at its start, never its end, so its own domain and
// the port show whatever it begins with (registry.npmjs.org.<padding>.attacker.net). The address shows it whole.
const TITLE_HOST = 60;
const ending = (host: string) => (host.length > TITLE_HOST ? `…${host.slice(1 - TITLE_HOST)}` : host);

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
      details: [code(lined("Command", request.command), request.command, "\n\t"), ...(request.workdir === null ? [] : [code("In", request.workdir)])],
      height: 380,
    };
  }
  if (request.kind === "change") {
    const file = code("File", inFolder(chat.folder, request.path));
    // A path outside the folder is named whole, and its file host refuses it.
    const where = inside(chat.folder, request.path) ? ` in ${folder}` : "";
    if (request.action === "delete") {
      return { ...OPERATION, title: `Delete ${named(request.path)}?`, lead: `${asker(chat)} wants to delete this file${where}.`, details: [file], height: 300 };
    }
    const bytes = request.bytes ?? 0;
    const { preview } = request;
    const content: PromptDetail = preview === null
      ? { label: `New content, ${sizeOf(bytes)}`, value: "Not text.", code: false, keep: "" }
      : code(preview.cut ? `The first ${sizeOf(PREVIEW_BYTES)} of ${sizeOf(bytes)}` : `New content, ${sizeOf(bytes)}`, preview.text, "\n\t");
    return {
      ...OPERATION, title: `Write ${named(request.path)}?`, lead: `${asker(chat)} wants to write ${sizeOf(bytes)} to this file${where}.`, details: [file, content], height: 420,
    };
  }
  if (request.kind === "input") {
    const command: PromptDetail = request.command === null
      ? { label: "To the command", value: `A command Surogate did not start in this session (${request.process}).`, code: false, keep: "" }
      : code(lined("To the command", request.command), request.command, "\n\t");
    return {
      ...OPERATION,
      title: "Type into a running command?",
      lead: `${asker(chat)} wants to send this input to a command running in ${folder}.`,
      details: [command, code("Input", request.data)],
      height: 380,
    };
  }
  if (request.kind === "browser") return browserPrompt(request);
  const address = `${request.host}:${request.port}`;
  const title = `Connect to ${ending(request.host)}:${request.port}?`;
  return {
    title,
    lead: `A command in ${folder} wants to connect to this address. Allow lets through the connections waiting now; later ones ask again.`,
    details: [code("Address", address)],
    notes: request.privateNetwork ? ["This address is on a private network, such as a home or office network, or a VPN."] : [],
    choice: null,
    buttons: [button("deny", "Deny"), button("allow_session", "Allow all its ports for this chat", true), button("allow", "Allow", true)],
    focus: "deny",
    cancel: "deny",
    enter: null,
    // Tall enough to show the title, the lead, the warning and the whole address as it opens: a title past
    // 30 characters wraps to three lines of 25 px, and a line of 19 px in the address's block holds about 40.
    height: 245 + (title.length > 30 ? 75 : 25) + (request.privateNetwork ? 70 : 0) + Math.ceil(address.length / 40) * 19,
  };
}

// An address's host, as an open prompt's title names it; the address whole when it has none.
const hostOf = (address: string): string => {
  try {
    return new URL(address).host || address;
  } catch {
    return address;
  }
};
// The most lines an address's block opens with; a longer one scrolls in it.
const MAX_ADDRESS_LINES = 12;

// What each browser operation's prompt says it would do in the page, and what its detail is: its title names where.
const BROWSER_ACTS: Record<Exclude<BrowserAction, "use" | "open">, { title: string; does: string; label: string }> = {
  script: { title: "Run a script in", does: "wants to run this script in the page open in its browser. A script can read the page and act on the site as you.", label: "Script" },
  click: { title: "Click in", does: "wants to click the page open in its browser, at this place.", label: "Where" },
  type: { title: "Type into", does: "wants to type this into the page open in its browser.", label: "Text" },
  press: { title: "Press keys in", does: "wants to press these keys in the page open in its browser.", label: "Keys" },
  drag: { title: "Drag in", does: "wants to drag along these points in the page open in its browser.", label: "Path" },
  other: { title: "Act in", does: "wants to act in the page open in its browser.", label: "Operation" },
};

// The site an act's page is on, as its title names it, cut at its start as an open's is: "the page" for one on none.
const siteOf = (page: string | null | undefined): string => {
  if (!page || !/^https?:/.test(page)) return "the page";
  return ending(hostOf(page));
};

/** The browser's prompts (spec, Section 5): its first use in a chat, and each act in a chat that asks every time. */
function browserPrompt(request: Extract<ApprovalRequest, { kind: "browser" }>): PromptContent {
  const { chat } = request;
  if (request.action === "use") {
    return {
      title: `Let ${chat.agent} use a browser on this computer?`,
      lead: `${asker(chat)} wants to open web pages in a browser on this computer, for this chat. It has a profile of its own, signed in to nothing of yours; what you sign in to there stays signed in for ${chat.agent}.`,
      details: [],
      notes: [
        "What its pages show is sent to the agent, and can stay in the conversation.",
        "It cannot reach this computer's own services or your private networks.",
      ],
      choice: null,
      buttons: [button("deny", "Deny"), button("allow_session", "Allow for this chat", true)],
      focus: "deny",
      cancel: "deny",
      enter: null,
      height: 360,
    };
  }
  if (request.action === "open") {
    // Named by its host, cut at its start as a network prompt's is, and tall enough for the whole address as it opens.
    const title = `Open ${ending(hostOf(request.detail))}?`;
    return {
      ...OPERATION,
      title,
      lead: `${asker(chat)} wants to open this address in its browser on this computer.`,
      details: [code("Address", request.detail)],
      height: 245 + (title.length > 30 ? 75 : 25) + Math.min(Math.ceil(request.detail.length / 40), MAX_ADDRESS_LINES) * 19,
    };
  }
  const act = BROWSER_ACTS[request.action];
  // The page it acts in, whole: a page can send itself to another site after it was opened.
  const page = request.page === undefined
    ? []
    : [request.page === null ? { label: "Page", value: "Not known: the browser did not say in time", code: false, keep: "" } : code("Page", request.page)];
  const title = `${act.title} ${siteOf(request.page)}?`;
  return {
    ...OPERATION,
    title,
    lead: `${asker(chat)} ${act.does}`,
    // A script's lines are shown as they are, with how many there are: what follows its first can be out of view.
    details: [...page, request.action === "script" ? code(lined(act.label, request.detail), request.detail, "\n\t") : code(act.label, request.detail)],
    height: (request.action === "script" ? 420 : 340) + (title.length > 30 ? 50 : 0)
      + (request.page ? 25 + Math.min(Math.ceil(request.page.length / 40), MAX_ADDRESS_LINES) * 19 : request.page === null ? 45 : 0),
  };
}

/** The desktop's own confirmation before a chat works freely (spec, Section 4). */
export function freeMode(chat: ChatLabel): PromptContent {
  return {
    title: `Let ${chat.agent} work freely in ${named(chat.folder)}?`,
    lead: "It will run commands, change files and type into its commands in this folder without asking you first. It still asks before it connects to an address off the package hosts.",
    details: [code("Folder", chat.folder)],
    notes: [],
    choice: null,
    buttons: [button("keep", "Keep asking"), button("free", "Work freely", true)],
    focus: "keep",
    cancel: "keep",
    enter: null,
    height: 300,
  };
}

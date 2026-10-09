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
  focus: string; // the button focused as it opens
  cancel: string; // Escape's, and a window closed some other way
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

// A name the page sent, as a field of its own, and the height its lines take.
const plain = (label: string, value: string): PromptDetail => ({ label, value, code: false, keep: "" });
const tall = (value: string) => 26 + Math.ceil(value.length / 45) * 19;

/** The folder sheet: the folder, its mode, and what the user should know; or why it cannot be used. */
export function folderSheet(sheet: FolderSheet): PromptContent {
  const name = named(sheet.folder);
  // A project's thread is named under its folder: the page's words, set as text.
  const details = [code("Folder", sheet.folder)];
  let room = 0;
  if (sheet.thread !== null) {
    details.push(plain("Project", sheet.thread.project), plain("Thread", sheet.thread.thread));
    room = tall(sheet.thread.project) + tall(sheet.thread.thread);
  }
  if (sheet.refusal !== null) {
    return {
      title: `${name} cannot be used`,
      lead: `${sheet.agent} cannot work in this folder: ${sheet.refusal}. Choose another folder.`,
      details,
      notes: [],
      choice: null,
      buttons: [button("cancel", "Cancel"), button("change", "Change…")],
      // Change opens the system's folder dialog over the app: Cancel is the one that does nothing.
      focus: "cancel",
      cancel: "cancel",
      height: 280 + room,
    };
  }
  return {
    title: `Work in ${name}?`,
    lead: `${sheet.agent} will read and change the files in this folder, and run commands there. It works in the cloud: the files it reads and what its commands print are sent to it, and can stay in the conversation.`,
    details,
    notes: sheet.links ? [linked(sheet.links)] : [],
    choice: { ...MODES, value: sheet.mode },
    buttons: [button("cancel", "Cancel"), button("change", "Change…"), button("accept", "Use this folder", true)],
    // On Cancel, as every prompt starts on the button that changes nothing: a Return of a person who was typing
    // elsewhere binds no folder. The mode is theirs to pick, and Use this folder theirs to walk to.
    focus: "cancel",
    cancel: "cancel",
    height: (sheet.links ? 550 : 490) + room,
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
    if (request.download) {
      // A download's save says where the file came from: no tool of the agent's wrote it.
      const own = request.download === "user";
      return {
        ...OPERATION,
        // Its user's own asks in either mode, so there is no asking to stop.
        ...(own ? { buttons: [button("deny", "Deny"), button("allow", "Save", true)] } : {}),
        title: `Save ${named(request.path)}?`,
        // One taken for its user's says when it came, not who clicked: a page can start one by itself under their
        // hand, and one that comes just after they handed the browser back may have been asked for before.
        lead: own
          ? `This file was downloaded while you had control of ${chat.agent}'s browser, or just after you handed it back, ${sizeOf(bytes)}. Save it${where}? ${chat.agent} can read what is saved there.`
          : `A page in ${chat.agent}'s browser downloaded this file, ${sizeOf(bytes)}. Save it${where}?`,
        details: [file, content],
        height: 420,
      };
    }
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
const BROWSER_ACTS: Record<Exclude<BrowserAction, "use" | "port" | "open">, { title: string; does: string; label: string }> = {
  script: { title: "Run a script in", does: "wants to run this script in the page open in its browser. A script can read the page and act on the site as you.", label: "Script" },
  click: { title: "Click in", does: "wants to click the page open in its browser, at this place.", label: "Where" },
  down: { title: "Press the mouse in", does: "wants to press the mouse button in the page open in its browser, at this place, and hold it down.", label: "Where" },
  up: { title: "Release the mouse in", does: "wants to release the mouse button in the page open in its browser, at this place.", label: "Where" },
  type: { title: "Type into", does: "wants to type this into the page open in its browser.", label: "Text" },
  press: { title: "Press keys in", does: "wants to press these keys in the page open in its browser.", label: "Keys" },
  drag: { title: "Drag in", does: "wants to drag along these points in the page open in its browser.", label: "Path" },
  upload: { title: "Upload to", does: "wants to give these files to the page open in its browser. The site gets what they hold.", label: "File" },
  other: { title: "Act in", does: "wants to act in the page open in its browser.", label: "Operation" },
};

// The site an act's page is on, as its title names it, cut at its start as an open's is: "the page" for one on none.
const siteOf = (page: string | null | undefined): string => {
  if (!page || !/^https?:/.test(page)) return "the page";
  return ending(hostOf(page));
};

// The most a prompt's window is tall: what it shows past that scrolls in it.
const MAX_HEIGHT = 720;

/** The browser's prompts (spec, Section 5): its first use in a chat, and each act in a chat that asks every time. */
function browserPrompt(request: Extract<ApprovalRequest, { kind: "browser" }>): PromptContent {
  const { chat } = request;
  if (request.action === "use") {
    return {
      title: `Let ${chat.agent} use a browser on this computer?`,
      lead: `${asker(chat)} wants to open web pages in a browser on this computer, for this chat. It has a profile of its own, apart from your own browser; what you sign in to there stays signed in for ${chat.agent}, in its other chats too.`,
      details: [],
      notes: [
        "What its pages show is sent to the agent, and can stay in the conversation.",
        "It cannot reach this computer's own services or your private networks.",
      ],
      choice: null,
      buttons: [button("deny", "Deny"), button("allow_session", "Allow for this chat", true)],
      focus: "deny",
      cancel: "deny",
      height: 380,
    };
  }
  if (request.action === "port") {
    // The browser's one private destination (spec, Section 5): a port of the chat's own servers, for the chat or not at
    // all, since one page load makes many connections. What it says of who else reaches it is the proxy's rule (browser/proxy.ts).
    const held = request.held === undefined
      ? []
      : [`Another chat's server has port ${request.detail} in the browser now. Allowing this gives the port to this chat.`];
    return {
      title: `Let ${chat.agent}'s browser open port ${request.detail} of this chat's servers?`,
      lead: `${asker(chat)} wants to open a server it started for this chat, in the chat's sandbox, in its browser on this computer.`,
      details: [code("Address", `http://localhost:${request.detail}/`)],
      notes: [
        ...held,
        `Its browser is shared by all of ${chat.agent}'s chats: while this is allowed, a page open in any of them can reach this port too.`,
        "A page of another site cannot fetch from it, post to it, frame it or open a socket to it. It can still send a tab there, as a link does.",
        "It opens this chat's server only, never this computer's own services.",
      ],
      choice: null,
      buttons: [button("deny", "Deny"), button("allow_session", "Allow for this chat", true)],
      focus: "deny",
      cancel: "deny",
      height: 470 + held.length * 50,
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
  const room = (title.length > 30 ? 50 : 0)
    + (request.page ? 25 + Math.min(Math.ceil(request.page.length / 40), MAX_ADDRESS_LINES) * 19 : request.page === null ? 45 : 0);
  if (request.action === "upload") {
    // Each file is a field of its own, counted, with no special character of its path shown as itself, as a
    // download's File field: so a name cannot be made to read as two files, or as another's.
    const files = request.files ?? [];
    const fields = files.map((path, at) => code(files.length === 1 ? act.label : `${act.label} ${at + 1} of ${files.length}`, path));
    return {
      ...OPERATION,
      title,
      lead: `${asker(chat)} ${act.does}`,
      details: [...page, ...(fields.length > 0 ? fields : [plain("Files", "None")])],
      // Room for each path's lines, three at most: the window stays one a screen holds, and the rest scrolls in it.
      height: Math.min(MAX_HEIGHT, 280 + room + files.reduce((all, path) => all + 62 + Math.min(Math.ceil(path.length / 40), 3) * 19, 0)),
    };
  }
  return {
    ...OPERATION,
    title,
    lead: `${asker(chat)} ${act.does}`,
    // A script's lines are shown as they are, with how many there are: what follows its first can be out of view.
    details: [...page, request.action === "script" ? code(lined(act.label, request.detail), request.detail, "\n\t") : code(act.label, request.detail)],
    height: (request.action === "script" ? 420 : 340) + room,
  };
}

// What the hand back's confirmation is asked about.
export interface HandBackRequest {
  agent: string;
  // The chat the browser was taken over from is gone: deleted, or its folder forgotten on this computer.
  gone: boolean;
  // That chat's title, as the agent named it: null for one it names not, or did not name in time.
  title: string | null;
}

// The most of a chat's title its field shows, in characters: the agent's own words, cut at their end.
const TITLE_CHAT = 60;
const cut = (title: string): string => {
  const characters = [...title];
  return characters.length > TITLE_CHAT ? `${characters.slice(0, TITLE_CHAT - 1).join("")}…` : title;
};

/**
 * The desktop's own confirmation before the agent drives its browser again (spec, Section 5). Keep
 * control first and focused, and what Escape and a closed window answer; Hand back is held back until
 * the input protection has passed, so a press its user began for the page answers nothing here.
 */
export function handBack(request: HandBackRequest): PromptContent {
  // The chat's title is the agent's words: in a field of its own, cut short, never among the prompt's own.
  const details = request.gone || request.title === null ? [] : [code("Chat", cut(request.title))];
  return {
    title: `Hand the browser back to ${request.agent}?`,
    // The browser is one for every chat of the agent's here: what is handed back is every chat's.
    lead: `It will act in its browser on this computer again, in every chat. ${request.gone ? "The chat it was taken over from is gone." : "It was taken over from this chat."}`,
    details,
    notes: [],
    choice: null,
    buttons: [button("keep", "Keep control"), button("hand_back", "Hand back", true)],
    focus: "keep",
    cancel: "keep",
    // With room for the field's two lines, which the longest title it shows takes: one size for any title.
    height: details.length > 0 ? 300 : 230,
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
    height: 300,
  };
}

// The Settings dialog's page, over the dimmed window: a nav searched by label, and
// sections of rows, each a label, a description and its control. All text comes from
// the main process and is set with textContent, or with showText where it may hold the
// user's paths or QEMU's words.

import { asShown } from "../text.js";
import { byId, fillIcons, markTheme, showText } from "./ui.js";

interface Appearance {
  theme: "system" | "light" | "dark";
  textSize: string;
  transcriptWidth: string;
  motion: string;
}

interface State {
  browser: { choice: string; rows: Array<{ value: string; label: string; disabled: boolean }>; none: boolean; failure: string | null; held: boolean };
  appearance: Appearance;
  preferences: Record<string, "on" | "off">;
  startAtLoginRefused: string | null;
  account: { name: string; email: string } | null;
  computer: { name: string; connection: string; added: string | null; organisation: string | null; agents: string[] };
  links: { usage: boolean };
  sandbox: { text: string; actions: Array<"retry" | "log" | "check"> };
}

// A folder this computer's chats work on, and each chat on it (folders.ts).
interface Folder {
  folder: string;
  chats: Array<{
    root: string; title: string; mode: "free" | "ask"; hosts: string[]; browser: boolean; processes: Array<{ id: string; command: string }>;
  }>;
}

interface Settings {
  state(): Promise<State>;
  folders(): Promise<Folder[]>;
  takeBack(root: string, host: string): Promise<void>;
  takeBrowserBack(root: string): Promise<void>;
  handBrowserBack(): Promise<boolean>;
  stop(root: string, id: string): Promise<void>;
  set(key: string, value: string): Promise<void>;
  link(which: "usage"): Promise<void>;
  sandbox(action: "retry" | "log" | "check"): Promise<void>;
  close(): Promise<void>;
  onChanged(listener: () => void): () => void;
  onShow(listener: (section: string) => void): () => void;
}

const settings = (globalThis as unknown as { surogateSettings: Settings }).surogateSettings;

markTheme();
fillIcons();

let section = "general";

function show(name: string): void {
  section = name;
  for (const item of document.querySelectorAll<HTMLElement>(".settings-nav [data-section]")) {
    item.classList.toggle("selected", item.dataset.section === name);
  }
  for (const found of document.querySelectorAll<HTMLElement>("section[data-section]")) found.hidden = found.dataset.section !== name;
}

// A group with nothing to show, by search or for want of a link, goes with its heading.
function hideEmptyGroups(): void {
  for (const group of document.querySelectorAll<HTMLElement>(".nav-group")) {
    group.hidden = [...group.querySelectorAll<HTMLElement>(".item")].every((item) => item.hidden);
  }
}

// A nav item stays when its label or one of its section's rows matches; the rows that do not match go.
function search(): void {
  const query = byId<HTMLInputElement>("settings-search").value.trim().toLowerCase();
  const matches = (text: string | undefined) => query === "" || (text ?? "").toLowerCase().includes(query);
  let first: string | null = null;
  for (const item of document.querySelectorAll<HTMLElement>(".settings-nav .item")) {
    const rows = [...document.querySelectorAll<HTMLElement>(`section[data-section="${item.dataset.section}"] .row`)];
    for (const row of rows) row.hidden = !matches(row.dataset.label) && !matches(item.textContent ?? "");
    item.hidden = !matches(item.textContent ?? "") && !rows.some((row) => !row.hidden);
    if (!item.hidden && item.dataset.section && first === null) first = item.dataset.section;
  }
  hideEmptyGroups();
  const current = document.querySelector<HTMLElement>(`.settings-nav [data-section="${section}"]`);
  if (current?.hidden && first !== null) show(first);
}

const date = (iso: string | null) =>
  iso === null ? "Not registered" : new Date(iso).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });

// What Electron puts before the main process's words: the name of the call it invoked.
const INVOKED = /^Error invoking remote method '[^']*': (?:Error: )?/;
const said = (error: unknown): string => (error instanceof Error ? error.message : String(error)).replace(INVOKED, "");

// Why the user's last Take back or Stop failed: null once one goes through.
let failure: string | null = null;

// A line under a chat: what it holds, as text, and the button that ends it, named by what it ends, as it is shown.
function line(text: string, action: string, name: string, act: () => Promise<void>): HTMLElement {
  const held = document.createElement("span");
  held.className = "line";
  const what = document.createElement("span");
  showText(what, text);
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = action;
  button.setAttribute("aria-label", `${action} ${asShown(name)}`);
  // Drawn again either way: a list that changed meanwhile shows what holds, and a refusal is said above it.
  button.addEventListener("click", () => void act().then(() => {
    failure = null;
  }, (error: unknown) => {
    failure = `Surogate did not ${action.toLowerCase()} ${name}: ${said(error)}.`;
  }).finally(render));
  held.append(what, button);
  return held;
}

// A chat's row: its title, as text, its mode, each host its user let it reach, the browser if it may use it, and
// each background process it runs.
// Its title is what a search finds it by.
function chatRow(chat: Folder["chats"][number]): HTMLElement {
  const row = document.createElement("div");
  row.className = "row";
  row.dataset.label = chat.title;
  const label = document.createElement("div");
  label.className = "label";
  const title = document.createElement("span");
  showText(title, chat.title);
  const mode = document.createElement("span");
  mode.className = "desc";
  mode.textContent = chat.mode === "free" ? "Works freely" : "Asks every time";
  label.append(
    title,
    mode,
    ...chat.hosts.map((host) => line(`Reaches ${host}, on every port`, "Take back", host, () => settings.takeBack(chat.root, host))),
    ...(chat.browser
      ? [line("Uses the browser on this computer", "Take back", "the browser on this computer", () => settings.takeBrowserBack(chat.root))]
      : []),
    ...chat.processes.map(({ id, command }) => line(`Runs ${command}`, "Stop", command, () => settings.stop(chat.root, id))),
  );
  row.append(label);
  return row;
}

async function renderFolders(): Promise<void> {
  // A list that cannot be read, as on a computer the agent revoked, says why in its place.
  let folders: Folder[] = [];
  let unread: string | null = null;
  try {
    folders = await settings.folders();
  } catch (error) {
    unread = said(error);
  }
  showText(byId("folders-failed"), unread ?? failure ?? "");
  byId("folders-none").hidden = folders.length > 0 || unread !== null;
  byId("folders").replaceChildren(...folders.map((folder) => {
    const group = document.createElement("div");
    group.className = "folder";
    const path = document.createElement("h3");
    path.className = "folder-path";
    showText(path, folder.folder);
    group.append(path, ...folder.chats.map(chatRow));
    return group;
  }));
  // Rows drawn since the search was typed are searched too.
  search();
}

async function render(): Promise<void> {
  void renderFolders();
  const state = await settings.state();
  const chosen: Record<string, string> = { ...state.appearance, ...state.preferences };
  for (const control of document.querySelectorAll<HTMLElement>("[data-setting]")) {
    for (const option of control.querySelectorAll<HTMLElement>("[data-value]")) {
      option.setAttribute("aria-pressed", String(option.dataset.value === chosen[control.dataset.setting ?? ""]));
    }
  }
  const browser = byId<HTMLSelectElement>("browser");
  browser.replaceChildren(...state.browser.rows.map(({ value, label, disabled }) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.disabled = disabled;
    return option;
  }));
  browser.value = state.browser.choice;
  const note = byId("browser-note");
  note.textContent = state.browser.failure
    ?? (state.browser.none ? "No supported browser is installed. Install Google Chrome, Microsoft Edge, Brave or Vivaldi, or choose one with Custom…. The Snap build of Chromium is not supported." : "");
  note.hidden = note.textContent === "";
  // Held from a chat that is gone: handed back here.
  byId("browser-held").hidden = !state.browser.held;
  // Held again: what was said of the last hand back from here is true no more.
  if (state.browser.held) byId("browser-handed-back").hidden = true;
  // A build that cannot start at login says why, and its On does nothing; its Off still removes an entry already there.
  const refused = byId("login-refused");
  refused.textContent = state.startAtLoginRefused ?? "";
  refused.hidden = state.startAtLoginRefused === null;
  document.querySelector<HTMLButtonElement>('[data-setting="startAtLogin"] [data-value="on"]')!.disabled = !refused.hidden;
  byId("email").textContent = state.account?.email ?? "Not signed in";
  byId("name").textContent = state.account?.name ?? "";
  byId("organisation").textContent = state.computer.organisation ?? "";
  byId("computer").textContent = state.computer.name;
  byId("connection").textContent = state.computer.connection;
  byId("added").textContent = date(state.computer.added);
  byId("agents").textContent = state.computer.agents.join(", ");
  showText(byId("sandbox"), state.sandbox.text);
  byId("sandbox-log").hidden = !state.sandbox.actions.includes("log");
  byId("sandbox-retry").hidden = !state.sandbox.actions.includes("retry");
  byId("sandbox-check").hidden = !state.sandbox.actions.includes("check");
  // A link the agent lacks goes for good: no search brings it back.
  for (const link of document.querySelectorAll<HTMLElement>("[data-link]")) {
    if (!state.links[link.dataset.link as "usage"]) link.remove();
  }
  hideEmptyGroups();
}

for (const item of document.querySelectorAll<HTMLElement>(".settings-nav [data-section]")) {
  item.addEventListener("click", () => show(item.dataset.section ?? "general"));
}
for (const link of document.querySelectorAll<HTMLElement>("[data-link]")) {
  link.addEventListener("click", () => void settings.link(link.dataset.link as "usage"));
}
for (const control of document.querySelectorAll<HTMLElement>("[data-setting]")) {
  for (const option of control.querySelectorAll<HTMLElement>("[data-value]")) {
    option.addEventListener("click", () => void settings.set(control.dataset.setting ?? "", option.dataset.value ?? "").then(render));
  }
}
// The main process acts only on a button its line shows.
byId("sandbox-log").addEventListener("click", () => void settings.sandbox("log"));
byId("sandbox-retry").addEventListener("click", () => void settings.sandbox("retry"));
byId("sandbox-check").addEventListener("click", () => void settings.sandbox("check"));
// Custom… opens the system's dialog: the page shows the choice kept once the main process answers.
// Asked in the desktop's own confirmation, over this page: whatever its answer, the page is drawn anew when it is given.
// Handed back from here, no agent takes a turn for it, as one does at a hand back from the chat that held the
// browser: the page says so, for whoever expects the agent to go on.
byId("browser-hand-back").addEventListener("click", () => void settings.handBrowserBack().then((handed) => {
  if (handed) byId("browser-handed-back").hidden = false;
}, () => {}));
byId<HTMLSelectElement>("browser").addEventListener("change", (event) => {
  void settings.set("browser", (event.target as HTMLSelectElement).value).then(render, render);
});
byId("settings-search").addEventListener("input", search);
byId("close").addEventListener("click", () => void settings.close());
byId("backdrop").addEventListener("click", () => void settings.close());
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") void settings.close();
});
settings.onChanged(() => void render());
// Opened on a section, or shown one while open, as the agent's page opens Browser: one the nav has, or none.
const open = (name: string): void => {
  if (document.querySelector(`.settings-nav [data-section="${CSS.escape(name)}"]`)) show(name);
};
settings.onShow(open);
open(location.hash.slice(1));
void render().then(() => byId("settings-search").focus());

// The main window's own page: the sidebar, the centre's header and pages, and the
// Overview pane. The agent's web client is drawn over the centre's hole, in a view of
// its own. All text comes from the main process and is set with textContent, or with showText
// where it may hold the user's paths or QEMU's words.

import { ago, asShown } from "../text.js";
import { aged, byId, fillIcons, freshen, icon, keepFocus, markTheme, projectMark, showText } from "./ui.js";

// A project, as Section 12's ProjectSummary has it.
interface ProjectRow {
  id: string;
  name: string;
  createdAt: string;
  updatedAt: string;
  waiting: number;
  working: number;
}

// A thread, as Section 12's ThreadRow has it.
interface ThreadRow {
  id: string;
  title: string;
  group: "waiting" | "working" | "idle" | "resolved";
  reason: "question" | "approval" | "failed" | "computer" | "files" | null;
  statusLine: string | null;
  progress: { done: number; total: number } | null;
  files: Array<{ kind: "file" | "artifact"; label: string; ref: string; landing: "landed" | "redoing" | "not_merged" | "undone" | null }>;
  place: { kind: "cloud" } | { kind: "device"; deviceName: string; online: boolean };
  updatedAt: string;
}

// A version of a file, as Section 13's FileVersion has it; by null: someone the app has no name for.
interface Version {
  id: string;
  path: string;
  by: { kind: "you" } | { kind: "thread"; title: string } | { kind: "routine"; name: string } | null;
  at: string;
  change: "added" | "changed" | "deleted" | "restored" | "undone";
  merged: boolean;
  available: boolean;
}

interface State {
  first: boolean; // no agent yet: the first run fills the window
  agent: { name: string; desktopSessions: boolean } | null; // desktopSessions: its web client lists this computer in Settings → Devices
  view: { kind: "web" } | { kind: "projects" } | { kind: "project"; id: string; name: string; thread: { id: string; title: string } | null };
  projects: ProjectRow[]; // as the page listed them
  failure: string | null; // why what the user last asked for, a project or a thread's resolve, did not happen
  overview: {
    threads: ThreadRow[]; // last active first
    library: Array<{
      path: string; origin: "added" | "produced"; threadId: string | null; size: number | null; updatedAt: string | null;
      place: ThreadRow["place"];
    }>;
    routines: Array<{ name: string; scheduleDisplay: string }>;
    // Its files that are gone, each the version that deleted it; more: others, deleted longer ago, may not be listed.
    deleted: { files: Version[]; more: boolean };
  } | null;
  reading: { id: string; title: string } | null; // the thread read in the pane, beside the project's conversation
  // A file's History, shown in the Library in place of its files: its versions, null until they are
  // read, why what was last asked of it did not happen, the version on its way to be saved, the version
  // on its way back as the file, and what the last Restore did.
  history: {
    path: string; versions: Version[] | null; failure: string | null; opening: string | null; restoring: string | null;
    result: { applied: string[]; skipped: Array<{ path: string; by: Version["by"] }>; pickedUp: string[] } | null;
  } | null;
  device: { text: string; status: string | null } | null;
  account: { name: string; email: string; userId: string; orgId: string } | null;
  links: string[]; // the user menu's links the app knows for this agent
  unreachable: string | null;
  notice: string | null;
  signIn: { needed: boolean; pending: boolean; failure: string | null }; // the app's own sign-in, in the system browser
  deviceAction: { text: string; button: string; action: "sign-in" | "restore" } | null; // what the user can do about this computer
  quitting: number | null; // while a quit waits for the threads working on this computer: how many
  sandbox: { text: string; said: string; actions: Array<"retry" | "log" | "check">; ready: boolean }; // what stops the agent's commands, or slows them
  update: { text: string; button: string | null } | null; // a newer Surogate, and what the user can do about it
}

interface Shell {
  state(): Promise<State>;
  connect(address: string): Promise<string | null>; // why it was refused, or null
  signIn(): Promise<void>;
  signOut(): Promise<void>;
  remove(): Promise<void>;
  restore(): Promise<void>;
  go(path: string): Promise<void>;
  projects(): Promise<void>;
  project(id: string): Promise<void>;
  thread(id: string): Promise<void>;
  read(id: string | null): Promise<void>;
  resolve(id: string): Promise<void>;
  reopen(id: string): Promise<void>;
  history(path: string): Promise<void>;
  closeHistory(): Promise<void>;
  openVersion(id: string): Promise<void>;
  restoreVersion(id: string): Promise<void>;
  back(): Promise<void>;
  forward(): Promise<void>;
  reload(): Promise<void>;
  place(hole: { x: number; y: number; width: number; height: number }): Promise<void>;
  placePane(hole: { x: number; y: number; width: number; height: number }): Promise<void>;
  menu(which: "app" | "project"): Promise<void>;
  settings(): Promise<void>;
  newProject(): Promise<void>;
  projectSettings(): Promise<void>;
  quitNow(): Promise<void>;
  link(which: string): Promise<void>;
  sandbox(action: "retry" | "log" | "check"): Promise<void>;
  update(): Promise<void>;
  onChanged(listener: () => void): () => void;
  focusPane(): Promise<void>;
  onPaneLeft(listener: (to: string) => void): () => void;
}

const shell = (globalThis as unknown as { surogateShell: Shell }).surogateShell;

markTheme();
fillIcons();

const ENDED = ["revoked", "unauthenticated", "superseded", "update_required"];

let last: State | null = null;

function element<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text?: string): HTMLElementTagNameMap[K] {
  const made = document.createElement(tag);
  made.className = className;
  if (text !== undefined) made.textContent = text;
  return made;
}

function button(className: string, text: string, onClick: () => void): HTMLButtonElement {
  const made = element("button", className, text);
  made.type = "button";
  made.addEventListener("click", onClick);
  return made;
}

// A project in the sidebar, with a dot while a thread waits on the user.
function group(project: ProjectRow, selected: boolean): HTMLElement {
  const wrapper = element("div", "group");
  wrapper.dataset.project = project.id;
  const open = button(selected ? "item project selected" : "item project", "", () => void shell.project(project.id));
  open.dataset.focus = `project:${project.id}`;
  const mark = projectMark(project.id);
  open.append(mark, element("span", "name", project.name));
  if (project.waiting > 0) {
    // On the mark, as Claude Desktop puts it.
    mark.append(element("span", "waiting"));
    open.setAttribute("aria-label", `${project.name}, waiting on you`);
  }
  if (selected) open.setAttribute("aria-current", "page");
  wrapper.append(open);
  return wrapper;
}

function card(project: ProjectRow): HTMLElement {
  const made = button("card", "", () => void shell.project(project.id));
  made.dataset.project = project.id;
  made.dataset.focus = `card:${project.id}`;
  made.append(projectMark(project.id, 18), element("span", "name", project.name), aged(element("span", "age"), project.updatedAt, "long"));
  return made;
}

// The sidebar's projects that match its search; its heading only over some.
function filterSidebar(): void {
  const query = byId<HTMLInputElement>("search").value.trim().toLowerCase();
  let shown = 0;
  for (const found of document.querySelectorAll<HTMLElement>("#projects .group")) {
    found.hidden = query !== "" && !(found.querySelector(".name")?.textContent ?? "").toLowerCase().includes(query);
    if (!found.hidden) shown++;
  }
  byId("projects-head").hidden = shown === 0;
}

// Last active first: the sidebar's order, and the Projects page's Activity.
const byActivity = (a: ProjectRow, b: ProjectRow) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt);

const SORTS: Record<string, (a: ProjectRow, b: ProjectRow) => number> = {
  activity: byActivity,
  created: (a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt),
  name: (a, b) => a.name.localeCompare(b.name),
};

function renderCards(): void {
  if (!last) return;
  const query = byId<HTMLInputElement>("project-search").value.trim().toLowerCase();
  const shown = last.projects.filter((project) => project.name.toLowerCase().includes(query))
    .sort(SORTS[byId<HTMLSelectElement>("sort").value]);
  byId("cards").replaceChildren(...shown.map(card));
  byId("no-projects").hidden = last.projects.length > 0;
  byId("no-match").hidden = last.projects.length === 0 || shown.length > 0;
}

const plural = (count: number, one: string, many: string): string => `${count} ${count === 1 ? one : many}`;
const kilobytes = (size: number | null): string => (size === null ? "" : ` · ${Math.max(1, Math.round(size / 1024))} KB`);
// A row's state in a word or two: what it waits for, or else its group.
const REASONS = { question: "Question", approval: "Approval", failed: "Failed", computer: "Computer away", files: "Files" } as const;
const GROUPS = { waiting: "Waiting", working: "Working", idle: "Idle", resolved: "Resolved" } as const;
let tab = "threads";
// A file's state in the project's files, after its name, where it has one to say.
const MARKS = { landed: null, redoing: "being redone", not_merged: "not merged", undone: "undone" } as const;

// A row's file: its name, and its mark after it. The name is data: with a mark it is shown as the
// app's prompts show text, so that none of its characters reorders or hides the mark, and a name
// too long for its row is cut before the mark.
function chip(file: ThreadRow["files"][number]): HTMLElement {
  const mark = file.landing ? MARKS[file.landing] : null;
  if (mark === null) return element("span", "chip", file.label);
  const made = element("span", "chip marked");
  made.append(element("span", "name", asShown(file.label)), element("span", "state", ` · ${mark}`));
  return made;
}

// How a version came to be, and who made it, in a line of the app's own words. A thread is said to
// be one, so that a thread titled "you" never reads as the user. A name from the history is data:
// shown as the app's prompts show text (asShown), so that none of its characters reorders or hides
// the words around it. Someone the app has no name for is not named.
const CHANGES = { added: "Added", changed: "Changed", deleted: "Deleted", restored: "Restored", undone: "Undone" } as const;
function what(version: Version, change: keyof typeof CHANGES = version.change): string {
  return version.by === null ? CHANGES[change] : `${CHANGES[change]} by ${who(version.by)}`;
}

function who(by: NonNullable<Version["by"]>): string {
  return by.kind === "you" ? "you" : by.kind === "thread" ? `the thread ${asShown(by.title)}` : `the routine ${asShown(by.name)}`;
}

// A file's History opens in the Library, in place of its files.
function openHistory(path: string): void {
  tab = "library";
  showTab();
  void shell.history(path);
}

// A thread's, or a file's, on the user's computer: a laptop, titled with the computer and whether it is online.
function laptop(place: ThreadRow["place"]): HTMLElement[] {
  if (place.kind !== "device") return [];
  const mark = element("span", "place");
  mark.title = `On ${place.deviceName}, which is ${place.online ? "online" : "offline"}`;
  mark.append(icon("laptop", 14));
  return [mark];
}

// A row reads its thread in the pane; the pane's Open shows it in the centre. One that waits on
// the user over a file that did not merge opens that file's History, the first such file's.
function threadRow(thread: ThreadRow): HTMLElement {
  const unmerged = thread.reason === "files" && thread.place.kind === "cloud"
    ? thread.files.find((file) => file.kind === "file" && file.landing === "not_merged") : undefined;
  const row = button("thread", "", () => void (unmerged ? openHistory(unmerged.ref) : shell.read(thread.id)));
  row.dataset.group = thread.group;
  row.dataset.thread = thread.id;
  row.dataset.focus = `thread:${thread.id}`;
  const title = element("span", "title", thread.title);
  title.append(...laptop(thread.place));
  const words = thread.reason ? REASONS[thread.reason] : GROUPS[thread.group];
  const side = element("span", "side");
  if (thread.progress) side.append(element("span", "progress", `${thread.progress.done}/${thread.progress.total}`));
  side.append(aged(element("span", "age"), thread.updatedAt));
  row.append(element("span", "mark"), title, element("span", "status", thread.statusLine ? `${words} · ${thread.statusLine}` : words), side);
  if (thread.files.length > 0) {
    const chips = element("span", "chips");
    for (const file of thread.files.slice(0, 2)) chips.append(chip(file));
    if (thread.files.length > 2) chips.append(element("span", "chip", `+${thread.files.length - 2}`));
    row.append(chips);
  }
  // Resolved threads come back with Reopen; any other is resolved from its row, a working one stopped first.
  const resolved = thread.group === "resolved";
  const act = button("act", resolved ? "Reopen" : "Resolve", () => void (resolved ? shell.reopen(thread.id) : shell.resolve(thread.id)));
  act.dataset.act = thread.id;
  act.dataset.focus = `act:${thread.id}`;
  act.setAttribute("aria-label", `${resolved ? "Reopen" : "Resolve"} ${thread.title}`);
  const item = element("li", "");
  item.append(row, act);
  return item;
}

function listRow(first: string, second: string, third: string, place: ThreadRow["place"] = { kind: "cloud" }): HTMLElement {
  const item = element("li", "file");
  const from = element("span", "from", second);
  from.append(...laptop(place));
  item.append(element("span", "path", first), from, element("span", "age", third));
  return item;
}

// A cloud file's row in the Library, which opens its History: one of its files, or of those deleted from them.
function historyRow(path: string, item: HTMLElement, list: "file" | "deleted" = "file"): HTMLElement {
  const open = button("file", "", () => openHistory(path));
  open.dataset.file = path;
  open.dataset.focus = `${list}:${path}`;
  open.setAttribute("aria-label", `History of ${asShown(path)}`);
  open.append(...item.childNodes);
  const row = element("li", "");
  row.append(open);
  return row;
}

// A version of the file whose History is shown: how it came to be and by whom, when, and whether
// it landed and is still kept. One still kept that left a file is opened, to be saved, and restored:
// none that is no longer kept, and no deletion, which left nothing to open or to bring back. While one
// is on its way no other is opened, and while one is restored no other is: their buttons wait,
// without their use, so that the keyboard stays where it is.
function versionRow(version: Version, opening: boolean, restoring: boolean): HTMLElement {
  const item = element("li", "version");
  item.dataset.version = version.id;
  const tags = [...(version.merged ? [] : ["Not merged"]), ...(version.available ? [] : ["No longer kept"])];
  const acts = element("span", "acts");
  if (version.available && version.change !== "deleted") {
    // One the History no longer shows, or shows as no longer kept, is refused there: the pane is drawn again with it.
    const act = (name: "open" | "restore", text: string, waiting: boolean, ask: (id: string) => Promise<void>) => {
      const made = button("act", text, () => {
        if (!waiting) void ask(version.id).catch(() => {});
      });
      made.dataset.act = name;
      made.dataset.focus = `version:${version.id}:${name}`;
      if (waiting) made.setAttribute("aria-disabled", "true");
      return made;
    };
    acts.append(
      act("open", "Open version", opening, (id) => shell.openVersion(id)), act("restore", "Restore", restoring, (id) => shell.restoreVersion(id)),
    );
  }
  item.append(element("span", "what", what(version)), aged(element("span", "age"), version.at), element("span", "from", tags.join(" · ")), acts);
  return item;
}

// What the last Restore did, in a line a file: what it restored, whose edit it recorded first, and
// each it left as it was, with who changed it since where the agent names them. Each name is data.
function told(result: NonNullable<State["history"]>["result"]): string[] {
  if (result === null) return [];
  const lines = result.applied.map((path) => `Restored ${asShown(path)}.`);
  lines.push(...result.pickedUp.map((path) => `Your changes to ${asShown(path)} were recorded first: they are a version in this History.`));
  for (const { path, by } of result.skipped) {
    lines.push(by === null
      ? `${asShown(path)} was left as it is.`
      : `${asShown(path)} was changed after this, by ${who(by)}. It was left as it is. Restore an earlier version from its History.`);
  }
  return lines.length === 0 ? ["The file is already this version."] : lines;
}

// The file whose History the pane showed when it was last drawn.
let historyShown: string | null = null;

// A file's History, in place of the Library's files. As it opens, the row that had the keyboard is
// hidden and the redraw gives it to the History's way back; as it closes, the keyboard goes back to
// the file's row, while the pane has it.
function renderHistory(state: State): void {
  const history = state.history;
  byId("file-history").hidden = history === null;
  byId("files").hidden = history !== null;
  if (history !== null) {
    byId("no-files").hidden = true;
    byId("deleted").hidden = true;
    showText(byId("history-path"), history.path);
    byId("history-told").replaceChildren(...told(history.result).map((line) => element("p", "", line)));
    byId("history-failure").hidden = history.failure === null;
    byId("history-failure").textContent = history.failure ?? "";
    byId("versions").replaceChildren(
      ...(history.versions ?? []).map((version) => versionRow(version, history.opening !== null, history.restoring !== null)),
    );
    byId("no-versions").hidden = history.versions?.length !== 0;
  }
  const was = historyShown;
  historyShown = history?.path ?? null;
  const inPane = document.activeElement === document.body || byId("panel").contains(document.activeElement);
  if (historyShown !== null || was === null || !inPane) return;
  document.querySelector<HTMLElement>(`[data-file="${CSS.escape(was)}"]`)?.focus();
}

function renderOverview(state: State): void {
  const overview = state.overview;
  const threads = overview?.threads ?? [];
  const waiting = threads.filter((found) => found.group === "waiting").length;
  const first = state.account?.name.trim().split(/\s+/)[0];
  byId("greeting").textContent = first ? `Welcome back, ${first}.` : "Welcome back.";
  byId("greeting-line").textContent = overview
    ? (waiting === 0 ? "Nothing is waiting on you." : `${plural(waiting, "thread is", "threads are")} waiting on you.`)
    : state.view.kind === "project" ? "Loading the project's threads…" : "Open a project to see its threads.";
  byId("thread-count").textContent = String(waiting);
  byId("thread-count").hidden = waiting === 0;
  // Waiting on you always shows, and says what it holds while it holds nothing; the other groups show only with threads.
  for (const section of document.querySelectorAll<HTMLElement>(".section")) {
    const rows = threads.filter((found) => found.group === section.dataset.group);
    section.hidden = rows.length === 0 && section.dataset.group !== "waiting";
    const desc = section.querySelector<HTMLElement>(":scope > .desc");
    if (desc) desc.hidden = rows.length > 0;
    section.querySelector(".count")!.textContent = String(rows.length);
    section.querySelector("ul")!.replaceChildren(...rows.map(threadRow));
  }
  const titles = new Map(threads.map((found) => [found.id, found.title]));
  const library = [...(overview?.library ?? [])].sort((a, b) => Date.parse(b.updatedAt ?? "") - Date.parse(a.updatedAt ?? ""));
  byId("files").replaceChildren(...library.map((entry) => {
    const item = listRow(
      entry.path,
      `${entry.origin === "added" ? "Added by you" : `From ${titles.get(entry.threadId ?? "") ?? "a thread"}`}${kilobytes(entry.size)}`,
      entry.updatedAt ? ago(entry.updatedAt) : "",
      entry.place,
    );
    // A file on a computer has its History there.
    return entry.place.kind === "cloud" ? historyRow(entry.path, item) : item;
  }));
  byId("no-files").hidden = library.length > 0;
  // The files that are gone, under those that are there. One that is among the files again, as one its
  // user uploaded anew and no landing has recorded yet, is not gone.
  const there = new Set(library.filter((entry) => entry.place.kind === "cloud").map((entry) => entry.path));
  const deleted = (overview?.deleted.files ?? []).filter((version) => !there.has(version.path));
  byId("deleted-files").replaceChildren(...deleted.map((version) => historyRow(
    version.path, listRow(version.path, what(version, "deleted"), ago(version.at)), "deleted",
  )));
  // Where the agent looked no further back, it is said, though none is listed.
  const more = overview?.deleted.more === true;
  byId("deleted").hidden = deleted.length === 0 && !more;
  byId("more-deleted").hidden = !more;
  const routines = overview?.routines ?? [];
  byId("routine-list").replaceChildren(...routines.map((routine) => listRow(routine.name, routine.scheduleDisplay, "")));
  document.querySelector<HTMLElement>('[data-tab="routines"]')!.hidden = routines.length === 0;
  if (tab === "routines" && routines.length === 0) tab = "threads";
  showTab();
  renderHistory(state);
}

// The thread read in the pane when it was last drawn.
let readingShown: string | null = null;

// The tab chosen; or, while a thread is read in the pane, its transcript in their place. As one
// opens there the keyboard goes to its Back, and as it closes, back to its row, while the pane
// has the keyboard: the row that had it is hidden meanwhile.
function showTab(): void {
  const reading = last?.reading ?? null;
  for (const each of document.querySelectorAll<HTMLElement>("[data-tab]")) each.setAttribute("aria-selected", String(each.dataset.tab === tab));
  document.querySelector<HTMLElement>(".panel .tabs")!.hidden = reading !== null;
  byId("threads").hidden = reading !== null || tab !== "threads";
  byId("library").hidden = reading !== null || tab !== "library";
  byId("routines").hidden = reading !== null || tab !== "routines";
  byId("reading").hidden = reading === null;
  // As last listed: a rename shows here too.
  byId("reading-title").textContent = reading ? (last?.overview?.threads.find((found) => found.id === reading.id)?.title ?? reading.title) : "";
  const was = readingShown;
  readingShown = reading?.id ?? null;
  const inPane = document.activeElement === document.body || byId("panel").contains(document.activeElement);
  if (readingShown === was || !inPane) return;
  if (reading) byId("reading-back").focus();
  else document.querySelector<HTMLElement>(`[data-thread="${CSS.escape(was ?? "")}"]`)?.focus();
}

// Drawn anew, the lists keep the keyboard where it was.
async function render(): Promise<void> {
  const state = await shell.state();
  keepFocus(() => draw(state));
}

function draw(state: State): void {
  state.projects.sort(byActivity);
  last = state;
  renderOverview(state);
  // The first run, and then the sign-in, fill the window until someone is signed in to the agent.
  document.body.classList.toggle("first", state.first || state.signIn.needed);
  byId("first-run").hidden = !state.first;
  byId("sign-in").hidden = state.first || !state.signIn.needed;
  byId("sign-in-title").textContent = state.agent ? `Sign in to ${state.agent.name}` : "";
  byId("sign-in-button").textContent = state.signIn.pending ? "Open the browser again" : "Continue in your browser";
  byId("sign-in-waiting").hidden = !state.signIn.pending;
  byId("sign-in-error").textContent = state.signIn.failure ?? "";
  const open = state.view.kind === "project" ? state.view : null;
  // The open project and thread as last listed: a rename shows in the header too.
  const name = state.projects.find((project) => project.id === open?.id)?.name ?? open?.name;
  const thread = open?.thread ? (state.overview?.threads.find((found) => found.id === open.thread?.id)?.title ?? open.thread.title) : undefined;
  byId("title").textContent = thread ?? name ?? (state.view.kind === "projects" ? "Projects" : state.agent?.name ?? "");
  byId("project-icon").replaceChildren(open ? projectMark(open.id) : icon("folder"));
  // A thread open in the centre: its project, as the way back.
  byId("to-project").hidden = !open?.thread;
  byId("to-project").textContent = open?.thread ? (name ?? "") : "";
  byId<HTMLButtonElement>("project-settings").disabled = !open;
  byId("overview-dot").hidden = !state.projects.some((project) => project.id === open?.id && project.waiting > 0);
  byId("projects").replaceChildren(...state.projects.map((project) => group(project, project.id === open?.id)));
  filterSidebar();
  byId("failure").hidden = state.failure === null;
  byId("failure").textContent = state.failure ?? "";
  // The Projects page fills the centre and the Overview's column, under the header's bare strip.
  const onPage = state.view.kind === "projects";
  document.body.classList.toggle("projects-view", onPage);
  byId("open-projects").classList.toggle("selected", onPage);
  if (onPage) byId("open-projects").setAttribute("aria-current", "page");
  else byId("open-projects").removeAttribute("aria-current");
  byId("projects-page").hidden = !onPage;
  renderCards();
  byId("user-name").textContent = state.account?.name ?? "Not signed in";
  byId("avatar").textContent = (state.account?.name ?? "").split(/\s+/).map((word) => word[0] ?? "").join("").slice(0, 2).toUpperCase();
  byId("user-email").textContent = state.account?.email ?? "Signing in…";
  for (const row of document.querySelectorAll<HTMLElement>("#user-menu [data-link]")) {
    row.hidden = !state.links.includes(row.dataset.link ?? "");
  }
  // Devices is the web client's Settings tab, which only an agent with local folders has.
  byId("user-menu").querySelector<HTMLElement>('[data-action="devices"]')!.hidden = state.agent?.desktopSessions !== true;
  // The divider under Plans and billing goes with it: no two dividers meet.
  byId("user-menu").querySelector<HTMLElement>("hr:last-of-type")!.hidden = !state.links.includes("billing");
  const device = byId("device");
  device.title = state.device?.text ?? "";
  device.classList.toggle("connected", state.device?.status === "connected");
  device.classList.toggle("ended", ENDED.includes(state.device?.status ?? ""));
  byId("notice").hidden = state.notice === null;
  byId("notice").textContent = state.notice ?? "";
  byId("device-action").hidden = state.deviceAction === null;
  byId("device-action-text").textContent = state.deviceAction?.text ?? "";
  byId("device-action-button").textContent = state.deviceAction?.button ?? "";
  byId("device-action-button").hidden = !state.deviceAction?.button;
  // Its words may hold QEMU's, which name the user's paths.
  byId("sandbox").hidden = state.sandbox.ready;
  showText(byId("sandbox-text"), state.sandbox.ready ? "" : state.sandbox.text);
  // Set only when it changes, so the live region speaks once a state: Ready too, once it follows
  // another, and nothing for a sandbox ready from the start.
  const live = byId("sandbox-said");
  const said = state.sandbox.ready && !live.textContent ? "" : state.sandbox.said;
  if (live.textContent !== said) showText(live, said);
  byId("sandbox-log").hidden = !state.sandbox.actions.includes("log");
  byId("sandbox-retry").hidden = !state.sandbox.actions.includes("retry");
  byId("sandbox-check").hidden = !state.sandbox.actions.includes("check");
  // The update's line is a live region that stays, empty, as the quit's does. Written only when its
  // words change, so that it speaks once a state.
  // Shown as text is: it may hold the root helper's own words, and the user's paths.
  const words = state.update?.text ?? "";
  if (byId("update-text").dataset.said !== words) {
    byId("update-text").dataset.said = words;
    showText(byId("update-text"), words);
    // Its longest words, with the notices beside it, are more than a short window's sidebar holds:
    // the notices then scroll among themselves, and the line that has just spoken is the one in sight.
    if (words !== "") byId("update").scrollIntoView({ block: "nearest" });
  }
  // While an update installs the line has no button. The one its user pressed stays where it is,
  // without its use, so that the keyboard is still on it when the line has one again.
  const button = byId("update-button");
  const label = state.update?.button ?? null;
  const pressed = label === null && state.update !== null && state.update !== undefined && document.activeElement === button;
  if (label !== null) button.textContent = label;
  else if (!pressed) button.textContent = "";
  button.hidden = label === null && !pressed;
  if (pressed) button.setAttribute("aria-disabled", "true");
  else button.removeAttribute("aria-disabled");
  // The quit's line is a live region that stays, empty, so that a screen reader hears it when it speaks.
  byId("quit-now").hidden = state.quitting === null;
  byId("quitting-text").textContent = state.quitting === null ? ""
    : `Quitting once ${state.quitting === 1 ? "1 thread working on this computer finishes" : `${state.quitting} threads working on this computer finish`}.`;
  byId("unreachable").hidden = state.unreachable === null;
  byId("headline").textContent = state.agent ? `Couldn't connect to ${state.agent.name}` : "";
  byId("why").textContent = state.unreachable ?? "";
}

// And it stays in sight when the notices' room changes, as when the window is made shorter.
new ResizeObserver(() => {
  if (byId("update-text").textContent) byId("update").scrollIntoView({ block: "nearest" });
}).observe(byId("notices"));

// One connection at a time: the form waits for the answer.
byId<HTMLFormElement>("connect").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = byId<HTMLFieldSetElement>("connecting");
  if (form.disabled) return;
  form.disabled = true;
  byId("error").textContent = "";
  void shell.connect(byId<HTMLInputElement>("address").value).then((refused) => {
    byId("error").textContent = refused ?? "";
  }).finally(() => {
    form.disabled = false;
  });
});
// Continue opens the browser; pressed again, it opens it once more, for a new sign-in.
byId("sign-in-button").addEventListener("click", () => void shell.signIn());
// An agent nobody can sign in to any more is removed from here, as from the user menu.
byId("sign-in-remove").addEventListener("click", () => void shell.remove());
byId("device-action-button").addEventListener("click", () => {
  if (last?.deviceAction?.action === "sign-in") void shell.signIn();
  else if (last?.deviceAction?.action === "restore") void shell.restore();
});
byId("quit-now").addEventListener("click", () => void shell.quitNow());
// The main process acts only on a button its line shows.
byId("sandbox-log").addEventListener("click", () => void shell.sandbox("log"));
byId("sandbox-retry").addEventListener("click", () => void shell.sandbox("retry"));
byId("sandbox-check").addEventListener("click", () => void shell.sandbox("check"));
byId("update-button").addEventListener("click", () => void shell.update());
byId("search").addEventListener("input", filterSidebar);
byId("project-search").addEventListener("input", renderCards);
byId("sort").addEventListener("change", renderCards);
byId("new").addEventListener("click", () => void shell.go("/chat"));
byId("new-project").addEventListener("click", () => void shell.newProject());
byId("project-settings").addEventListener("click", () => void shell.projectSettings());
byId("open-projects").addEventListener("click", () => void shell.projects());
byId("reading-back").addEventListener("click", () => void shell.read(null));
byId("history-back").addEventListener("click", () => void shell.closeHistory());
// Tab after the head's last control takes the keyboard into the transcript; it comes back from the transcript's
// edges: Shift+Tab from its first control to Open, Escape to Back.
byId("reading-open").addEventListener("keydown", (event) => {
  if (event.key !== "Tab" || event.shiftKey) return;
  event.preventDefault();
  void shell.focusPane();
});
shell.onPaneLeft((to) => byId(to === "open" ? "reading-open" : "reading-back").focus());
byId("reading-open").addEventListener("click", () => {
  if (last?.reading) void shell.thread(last.reading.id);
});
byId("to-project").addEventListener("click", () => {
  if (last?.view.kind === "project") void shell.project(last.view.id);
});
for (const each of document.querySelectorAll<HTMLElement>("[data-tab]")) {
  each.addEventListener("click", () => {
    tab = each.dataset.tab ?? "threads";
    showTab();
  });
}
for (const item of document.querySelectorAll<HTMLElement>("[data-path]")) {
  item.addEventListener("click", () => void shell.go(item.dataset.path ?? ""));
}
byId("back").addEventListener("click", () => void shell.back());
byId("forward").addEventListener("click", () => void shell.forward());
byId("refresh").addEventListener("click", () => void shell.reload());
byId("menu").addEventListener("click", () => void shell.menu("app"));
byId("project-menu").addEventListener("click", () => void shell.menu("project"));
byId("open-settings").addEventListener("click", () => void shell.settings());

// The user menu: a popover over the user row, as a menu button's menu. Opened, the keyboard is on
// its first row, and the arrows, Home and End move along the rows it offers now. Escape closes it and
// gives the keyboard back to the user row; Tab, a click elsewhere and its own rows close it.
const menuRows = () => [...document.querySelectorAll<HTMLButtonElement>("#user-menu [role=menuitem]")]
  .filter((row) => !row.disabled && row.checkVisibility());
const menu = (open: boolean) => {
  byId("user-menu").hidden = !open;
  byId("user").setAttribute("aria-expanded", String(open));
  if (open) menuRows()[0]?.focus();
};
byId("user").addEventListener("click", (event) => {
  event.stopPropagation();
  menu(byId("user-menu").hidden);
});
document.addEventListener("click", (event) => {
  if (!byId("user-menu").contains(event.target as Node)) menu(false);
});
const MOVES = new Map<string, (at: number, count: number) => number>([
  ["ArrowDown", (at) => at + 1], ["ArrowUp", (at) => at - 1], ["Home", () => 0], ["End", (_at, count) => count - 1],
]);
document.addEventListener("keydown", (event) => {
  if (byId("user-menu").hidden) return;
  if (event.key === "Escape") {
    menu(false);
    byId("user").focus();
    return;
  }
  if (event.key === "Tab") return menu(false);
  // The arrows are the menu's only while the keyboard is in it.
  if (!byId("user-menu").contains(document.activeElement)) return;
  const move = MOVES.get(event.key);
  if (!move) return;
  event.preventDefault();
  const rows = menuRows();
  const to = move(rows.indexOf(document.activeElement as HTMLButtonElement), rows.length);
  rows[(to + rows.length) % rows.length]?.focus();
});
// The conversation and Settings are views of their own: a click there reaches this page as its blur.
addEventListener("blur", () => menu(false));
for (const row of document.querySelectorAll<HTMLElement>("#user-menu [data-action]")) {
  const action = row.dataset.action ?? "";
  if (["usage", "help", "billing"].includes(action)) row.dataset.link = action;
  row.addEventListener("click", () => {
    menu(false);
    if (action === "settings") void shell.settings();
    // The account, the user's computers and desktop sign-ins are the agent's: its web client's Settings has them.
    else if (action === "account") void shell.go("/settings");
    else if (action === "devices") void shell.go("/settings?tab=devices");
    else if (action === "logout") void shell.signOut();
    else if (action === "remove") void shell.remove();
    else if (row.dataset.link) void shell.link(action);
  });
}

// The sidebar and the pane fold away, and come back from the centre's strip and header.
const fold = (name: string, folded: boolean, show: string) => {
  document.body.classList.toggle(name, folded);
  byId(show).hidden = !folded;
};
byId("hide-sidebar").addEventListener("click", () => fold("no-sidebar", true, "show-sidebar"));
byId("show-sidebar").addEventListener("click", () => fold("no-sidebar", false, "show-sidebar"));
// The Overview button opens and closes the pane; the pane's own close button closes it.
const pane = (open: boolean) => {
  document.body.classList.toggle("no-panel", !open);
  byId("overview").setAttribute("aria-pressed", String(open));
};
byId("overview").addEventListener("click", () => pane(document.body.classList.contains("no-panel")));
// Closed by its own button, the pane gives the keyboard to the one that opens it again.
byId("close-panel").addEventListener("click", () => {
  pane(false);
  byId("overview").focus();
});

// The web client is placed over the hole, wherever the layout puts it, and a thread read in the
// pane over the pane's: none while the pane is folded away or shows something else. Each is
// measured again as its size changes, and as the window's does, which moves the pane without
// resizing it once the pane is at its widest.
function placed(hole: string, place: (bounds: { x: number; y: number; width: number; height: number }) => Promise<void>): void {
  const measure = () => {
    const { x, y, width, height } = byId(hole).getBoundingClientRect();
    void place({ x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height) });
  };
  new ResizeObserver(measure).observe(byId(hole));
  window.addEventListener("resize", measure);
}
placed("hole", shell.place);
placed("pane-hole", shell.placePane);

// Ages tell the time gone by, between redraws too.
setInterval(freshen, 60_000);

shell.onChanged(() => void render());
void render();

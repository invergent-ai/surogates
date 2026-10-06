// The main window's own page: the sidebar, the centre's header and pages, and the
// Overview pane. The agent's web client is drawn over the centre's hole, in a view of
// its own. All text comes from the main process and is set with textContent only.

import { ago, byId, fillIcons, icon, markTheme } from "./ui.js";

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
  reason: "question" | "approval" | "failed" | "computer" | null;
  statusLine: string | null;
  progress: { done: number; total: number } | null;
  files: Array<{ label: string }>;
  place: { kind: "cloud" } | { kind: "device"; deviceName: string; online: boolean };
  updatedAt: string;
}

interface State {
  first: boolean; // no agent yet: the first run fills the window
  agent: { name: string } | null;
  view: { kind: "web" } | { kind: "projects" } | { kind: "project"; id: string; name: string; thread: { id: string; title: string } | null };
  projects: ProjectRow[]; // last active first
  overview: {
    threads: ThreadRow[]; // last active first
    library: Array<{ path: string; origin: "added" | "produced"; threadId: string | null; size: number | null; updatedAt: string | null }>;
    routines: Array<{ name: string; scheduleDisplay: string }>;
  } | null;
  device: { text: string; status: string | null } | null;
  account: { name: string; email: string; userId: string; orgId: string } | null;
  unreachable: string | null;
  notice: string | null;
}

interface Shell {
  state(): Promise<State>;
  connect(address: string): Promise<string | null>; // why it was refused, or null
  go(path: string): Promise<void>;
  projects(): Promise<void>;
  project(id: string): Promise<void>;
  thread(id: string): Promise<void>;
  back(): Promise<void>;
  forward(): Promise<void>;
  reload(): Promise<void>;
  place(hole: { x: number; y: number; width: number; height: number }): Promise<void>;
  menu(which: "app" | "project"): Promise<void>;
  onChanged(listener: () => void): () => void;
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
  open.append(icon("folder"), element("span", "name", project.name));
  if (project.waiting > 0) open.append(element("span", "waiting"));
  wrapper.append(open);
  return wrapper;
}

function card(project: ProjectRow): HTMLElement {
  const made = button("card", "", () => void shell.project(project.id));
  made.dataset.project = project.id;
  const badge = element("span", "badge");
  badge.append(icon("folder"));
  made.append(badge, element("span", "name", project.name), element("span", "age", ago(project.updatedAt, Date.now(), "long")));
  return made;
}

function filterSidebar(): void {
  const query = byId<HTMLInputElement>("search").value.trim().toLowerCase();
  for (const found of document.querySelectorAll<HTMLElement>("#projects .group")) {
    found.hidden = query !== "" && !(found.querySelector(".name")?.textContent ?? "").toLowerCase().includes(query);
  }
}

const SORTS: Record<string, (a: ProjectRow, b: ProjectRow) => number> = {
  activity: () => 0, // as listed: last active first
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
}

const plural = (count: number, one: string, many: string): string => `${count} ${count === 1 ? one : many}`;
const kilobytes = (size: number | null): string => (size === null ? "" : ` · ${Math.max(1, Math.round(size / 1024))} KB`);
// A row's state in a word or two: what it waits for, or else its group.
const REASONS = { question: "Question", approval: "Approval", failed: "Failed", computer: "Computer away" } as const;
const GROUPS = { waiting: "Waiting", working: "Working", idle: "Idle", resolved: "Resolved" } as const;
let tab = "threads";

function threadRow(thread: ThreadRow): HTMLElement {
  const row = button("thread", "", () => void shell.thread(thread.id));
  row.dataset.group = thread.group;
  row.dataset.thread = thread.id;
  const title = element("span", "title", thread.title);
  if (thread.place.kind === "device") {
    const place = element("span", "place");
    place.title = `On ${thread.place.deviceName}, which is ${thread.place.online ? "online" : "offline"}`;
    place.append(icon("laptop", 14));
    title.append(place);
  }
  const words = thread.reason ? REASONS[thread.reason] : GROUPS[thread.group];
  const side = element("span", "side");
  if (thread.progress) side.append(element("span", "progress", `${thread.progress.done}/${thread.progress.total}`));
  side.append(element("span", "age", ago(thread.updatedAt)));
  row.append(element("span", "mark"), title, element("span", "status", thread.statusLine ? `${words} · ${thread.statusLine}` : words), side);
  if (thread.files.length > 0) {
    const chips = element("span", "chips");
    for (const file of thread.files.slice(0, 2)) chips.append(element("span", "chip", file.label));
    if (thread.files.length > 2) chips.append(element("span", "chip", `+${thread.files.length - 2}`));
    row.append(chips);
  }
  const item = element("li", "");
  item.append(row);
  return item;
}

function listRow(first: string, second: string, third: string): HTMLElement {
  const item = element("li", "file");
  item.append(element("span", "path", first), element("span", "from", second), element("span", "age", third));
  return item;
}

function renderOverview(state: State): void {
  const overview = state.overview;
  const threads = overview?.threads ?? [];
  const waiting = threads.filter((found) => found.group === "waiting").length;
  byId("greeting").textContent = state.account ? `Welcome back, ${state.account.name.split(" ")[0]}.` : "Welcome back.";
  byId("greeting-line").textContent = !overview ? "Open a project to see its threads."
    : waiting === 0 ? "Nothing is waiting on you." : `${plural(waiting, "thread is", "threads are")} waiting on you.`;
  byId("thread-count").textContent = String(waiting);
  for (const section of document.querySelectorAll<HTMLElement>(".section")) {
    const rows = threads.filter((found) => found.group === section.dataset.group);
    section.querySelector(".count")!.textContent = String(rows.length);
    section.querySelector("ul")!.replaceChildren(...rows.map(threadRow));
  }
  const titles = new Map(threads.map((found) => [found.id, found.title]));
  const library = [...(overview?.library ?? [])].sort((a, b) => Date.parse(b.updatedAt ?? "") - Date.parse(a.updatedAt ?? ""));
  byId("files").replaceChildren(...library.map((entry) => listRow(
    entry.path,
    `${entry.origin === "added" ? "Added by you" : `From ${titles.get(entry.threadId ?? "") ?? "a thread"}`}${kilobytes(entry.size)}`,
    entry.updatedAt ? ago(entry.updatedAt) : "",
  )));
  byId("no-files").hidden = library.length > 0;
  const routines = overview?.routines ?? [];
  byId("routine-list").replaceChildren(...routines.map((routine) => listRow(routine.name, routine.scheduleDisplay, "")));
  document.querySelector<HTMLElement>('[data-tab="routines"]')!.hidden = routines.length === 0;
  if (tab === "routines" && routines.length === 0) tab = "threads";
  showTab();
}

function showTab(): void {
  for (const each of document.querySelectorAll<HTMLElement>("[data-tab]")) each.setAttribute("aria-selected", String(each.dataset.tab === tab));
  byId("threads").hidden = tab !== "threads";
  byId("library").hidden = tab !== "library";
  byId("routines").hidden = tab !== "routines";
}

async function render(): Promise<void> {
  const state = await shell.state();
  last = state;
  renderOverview(state);
  document.body.classList.toggle("first", state.first);
  byId("first-run").hidden = !state.first;
  const open = state.view.kind === "project" ? state.view : null;
  byId("title").textContent = open?.thread?.title ?? open?.name ?? (state.view.kind === "projects" ? "Projects" : state.agent?.name ?? "");
  // A thread open in the centre: its project, as the way back.
  byId("to-project").hidden = !open?.thread;
  byId("to-project").textContent = open?.thread ? open.name : "";
  byId("overview-dot").hidden = !state.projects.some((project) => project.id === open?.id && project.waiting > 0);
  byId("projects").replaceChildren(...state.projects.map((project) => group(project, project.id === open?.id)));
  filterSidebar();
  byId("open-projects").classList.toggle("selected", state.view.kind === "projects");
  byId("projects-page").hidden = state.view.kind !== "projects";
  renderCards();
  const device = byId("device");
  device.title = state.device?.text ?? "";
  device.classList.toggle("connected", state.device?.status === "connected");
  device.classList.toggle("ended", ENDED.includes(state.device?.status ?? ""));
  byId("notice").hidden = state.notice === null;
  byId("notice").textContent = state.notice ?? "";
  byId("unreachable").hidden = state.unreachable === null;
  byId("headline").textContent = state.agent ? `Couldn't connect to ${state.agent.name}` : "";
  byId("why").textContent = state.unreachable ? `Check your network connection (${state.unreachable})` : "";
}

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
byId("search").addEventListener("input", filterSidebar);
byId("project-search").addEventListener("input", renderCards);
byId("sort").addEventListener("change", renderCards);
// A new chat at the root is a new project: today's server keeps no projects of its own.
byId("new").addEventListener("click", () => void shell.go("/chat"));
byId("new-project").addEventListener("click", () => void shell.go("/chat"));
byId("open-projects").addEventListener("click", () => void shell.projects());
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
byId("close-panel").addEventListener("click", () => pane(false));

// The web client is placed over the hole, wherever the layout puts it.
new ResizeObserver(() => {
  const { x, y, width, height } = byId("hole").getBoundingClientRect();
  void shell.place({ x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height) });
}).observe(byId("hole"));

shell.onChanged(() => void render());
void render();

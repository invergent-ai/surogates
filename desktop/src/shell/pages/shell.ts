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

interface State {
  first: boolean; // no agent yet: the first run fills the window
  agent: { name: string } | null;
  view: { kind: "web" } | { kind: "projects" } | { kind: "project"; id: string; name: string };
  projects: ProjectRow[]; // last active first
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

async function render(): Promise<void> {
  const state = await shell.state();
  last = state;
  document.body.classList.toggle("first", state.first);
  byId("first-run").hidden = !state.first;
  const open = state.view.kind === "project" ? state.view : null;
  byId("title").textContent = open?.name ?? (state.view.kind === "projects" ? "Projects" : state.agent?.name ?? "");
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

// The web client is placed over the hole, wherever the layout puts it.
new ResizeObserver(() => {
  const { x, y, width, height } = byId("hole").getBoundingClientRect();
  void shell.place({ x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height) });
}).observe(byId("hole"));

shell.onChanged(() => void render());
void render();

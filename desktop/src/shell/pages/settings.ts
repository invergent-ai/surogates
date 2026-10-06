// The Settings dialog's page, over the dimmed window: a nav searched by label, and
// sections of rows, each a label, a description and its control. All text comes from
// the main process and is set with textContent only.

import { byId, fillIcons, markTheme } from "./ui.js";

interface Appearance {
  theme: "system" | "light" | "dark";
  textSize: string;
  transcriptWidth: string;
  motion: string;
}

interface State {
  appearance: Appearance;
  account: { name: string; email: string } | null;
  computer: { name: string; connection: string; added: string | null; organisation: string | null; agents: string[] };
  links: { usage: boolean };
}

interface Settings {
  state(): Promise<State>;
  set(key: string, value: string): Promise<void>;
  link(which: "usage"): Promise<void>;
  close(): Promise<void>;
  onChanged(listener: () => void): () => void;
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

async function render(): Promise<void> {
  const state = await settings.state();
  for (const control of document.querySelectorAll<HTMLElement>("[data-setting]")) {
    const chosen = state.appearance[control.dataset.setting as keyof Appearance];
    for (const option of control.querySelectorAll<HTMLElement>("[data-value]")) {
      option.setAttribute("aria-pressed", String(option.dataset.value === chosen));
    }
  }
  byId("email").textContent = state.account?.email ?? "Not signed in";
  byId("name").textContent = state.account?.name ?? "";
  byId("organisation").textContent = state.computer.organisation ?? "";
  byId("computer").textContent = state.computer.name;
  byId("connection").textContent = state.computer.connection;
  byId("added").textContent = date(state.computer.added);
  byId("agents").textContent = state.computer.agents.join(", ");
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
byId("settings-search").addEventListener("input", search);
byId("close").addEventListener("click", () => void settings.close());
byId("backdrop").addEventListener("click", () => void settings.close());
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") void settings.close();
});
settings.onChanged(() => void render());
void render().then(() => byId("settings-search").focus());

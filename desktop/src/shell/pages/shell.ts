// The main window's own page: the sidebar, the centre's header and pages, and the
// Overview pane. The agent's web client is drawn over the centre's hole, in a view of
// its own. All text comes from the main process and is set with textContent only.

import { byId, fillIcons, markTheme } from "./ui.js";

interface State {
  first: boolean; // no agent yet: the first run fills the window
  agent: { name: string } | null;
  device: { text: string; status: string | null } | null;
  account: { name: string; email: string; userId: string; orgId: string } | null;
  unreachable: string | null;
  notice: string | null;
}

interface Shell {
  state(): Promise<State>;
  connect(address: string): Promise<string | null>; // why it was refused, or null
  go(path: string): Promise<void>;
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

async function render(): Promise<void> {
  const state = await shell.state();
  document.body.classList.toggle("first", state.first);
  byId("first-run").hidden = !state.first;
  byId("title").textContent = state.agent?.name ?? "";
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

// The project dialog's page, over the dimmed window: a new project's name and goal, or the open
// project's settings and its archive. Every value comes from the main process, and is set as a
// field's value or with textContent only.

import { byId, markTheme } from "./ui.js";

type Tier = "basic" | "pro" | null;

interface Shown {
  name: string;
  goal: string | null;
  instructions: string;
  coordinatorTier: Tier;
  threadTier: Tier;
}

interface Fields {
  name: string;
  goal: string;
  instructions?: string;
  coordinatorTier?: Tier;
  threadTier?: Tier;
}

interface ProjectDialog {
  // A project's settings, or a new project's; and why the project could not be read, if it could not.
  state(): Promise<{ editing: boolean; project: Shown | null; refused: string | null }>;
  save(fields: Fields): Promise<string | null>; // why it was refused, or null once done
  archive(): Promise<string | null>;
  close(): Promise<void>;
}

const dialog = (globalThis as unknown as { surogateProject: ProjectDialog }).surogateProject;

markTheme();

let editing = false;
const value = (id: string) => byId<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(id).value;
const tier = (id: string): Tier => (value(id) === "basic" || value(id) === "pro" ? (value(id) as Tier) : null);

async function render(): Promise<void> {
  const { editing: shown, project, refused } = await dialog.state();
  editing = shown;
  byId("heading").textContent = editing ? "Project settings" : "New project";
  byId("save").textContent = editing ? "Save" : "Create project";
  byId("settings-only").hidden = !editing;
  // A project that could not be read is said so, with nothing to save or archive.
  byId("error").textContent = refused ?? "";
  byId("save").hidden = refused !== null;
  byId("archive").hidden = !editing || refused !== null;
  if (project) {
    byId<HTMLInputElement>("name").value = project.name;
    byId<HTMLTextAreaElement>("goal").value = project.goal ?? "";
    byId<HTMLTextAreaElement>("instructions").value = project.instructions;
    byId<HTMLSelectElement>("coordinator-tier").value = project.coordinatorTier ?? "";
    byId<HTMLSelectElement>("thread-tier").value = project.threadTier ?? "";
  }
  if (refused === null) return byId("name").focus();
  // Nothing to change: only Cancel is left, with the focus.
  for (const field of document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>("input, textarea, select")) {
    field.disabled = true;
  }
  byId("cancel").focus();
}

// One request at a time: the fields wait for its answer, which is said, or closes the dialog. A save
// says it is under way, as *busy*, on its button. The name has the focus back, which the fields'
// wait took from it, for the user to go on.
async function asked(request: () => Promise<string | null>, busy?: string): Promise<void> {
  const fields = byId<HTMLFieldSetElement>("fields");
  if (fields.disabled) return;
  fields.disabled = true;
  const save = byId("save");
  const idle = save.textContent;
  if (busy) {
    save.textContent = busy;
    byId("form").setAttribute("aria-busy", "true");
  }
  byId("error").textContent = "";
  try {
    byId("error").textContent = (await request()) ?? "";
  } catch (error) {
    byId("error").textContent = error instanceof Error ? error.message : String(error);
  } finally {
    fields.disabled = false;
    save.textContent = idle;
    byId("form").removeAttribute("aria-busy");
    byId("name").focus();
  }
}

byId<HTMLFormElement>("form").addEventListener("submit", (event) => {
  event.preventDefault();
  // Enter in a field submits too: with no Save, there is nothing to save.
  if (byId("save").hidden) return;
  const fields: Fields = { name: value("name"), goal: value("goal") };
  if (editing) {
    Object.assign(fields, { instructions: value("instructions"), coordinatorTier: tier("coordinator-tier"), threadTier: tier("thread-tier") });
  }
  void asked(() => dialog.save(fields), editing ? "Saving…" : "Creating…");
});
byId("archive").addEventListener("click", () => void asked(() => dialog.archive()));
byId("cancel").addEventListener("click", () => void dialog.close());
byId("backdrop").addEventListener("click", () => void dialog.close());
// An Escape that cancels an input method's composition is the composition's: the dialog stays, with what was typed.
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !event.isComposing) void dialog.close();
});
void render();

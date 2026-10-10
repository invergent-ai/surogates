// A file's History in the Overview's Library (spec, Section 13): its versions, who made each and
// when, read through the page's projects source. What it shows is the main process's, drawn by the
// window's page.

import type { FileVersion, ProjectsSource } from "../../../web/src/lib/projects-contract.js";

export interface ShownHistory {
  path: string;
  versions: FileVersion[] | null; // null until it is read
  failure: string | null; // why the last read of it did not happen, in the agent's words
}

type Source = Pick<ProjectsSource, "history">;

const reason = (error: unknown): string => (error instanceof Error ? error.message : String(error));

export class FileHistory {
  shown: ShownHistory | null = null;
  private project: string | null = null;
  private reads = 0;

  constructor(private readonly projects: Source, private readonly changed: () => void) {}

  /** Show *path*'s History in *project*: read now, and again whenever the project changes. */
  async open(project: string, path: string): Promise<void> {
    this.project = project;
    this.shown = { path, versions: null, failure: null };
    this.changed();
    await this.read();
  }

  close(): void {
    if (!this.shown) return;
    this.project = null;
    this.shown = null;
    this.changed();
  }

  /** Read the History shown again; an answer that comes after a later read, or a file closed since, applies nothing. */
  async read(): Promise<void> {
    const { project, shown } = this;
    if (!project || !shown) return;
    const mine = ++this.reads;
    const current = (): boolean => mine === this.reads && this.shown === shown;
    try {
      const versions = await this.projects.history(project, shown.path, { kind: "cloud" });
      if (!current()) return;
      shown.versions = versions;
      shown.failure = null;
    } catch (error) {
      if (!current()) return;
      shown.failure = reason(error);
    }
    this.changed();
  }
}

// A file's History in the Overview's Library (spec, Section 13): its versions, who made each and
// when, with Open version, through the page's projects source. What it shows is the main process's,
// drawn by the window's page; what the page asks of it is checked against what it shows.

import type { FileVersion, ProjectsSource } from "../../../web/src/lib/projects-contract.js";
import { TimedOut } from "./projects.js";

export interface ShownHistory {
  path: string;
  versions: FileVersion[] | null; // null until it is read
  failure: string | null; // why the last thing asked of it did not happen, in the agent's words
  opening: string | null; // the version on its way to be saved: no other is opened meanwhile
}

type Source = Pick<ProjectsSource, "history" | "openVersion">;

const reason = (error: unknown): string => (error instanceof Error ? error.message : String(error));

export class FileHistory {
  shown: ShownHistory | null = null;
  private project: string | null = null;
  private reads = 0;
  // The failure said is a read's own: the next read that succeeds takes it away.
  private unread = false;

  constructor(private readonly projects: Source, private readonly changed: () => void) {}

  /** Show *path*'s History in *project*: read now, and again whenever the project changes. */
  async open(project: string, path: string): Promise<void> {
    this.project = project;
    this.shown = { path, versions: null, failure: null, opening: null };
    this.unread = false;
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
      if (this.unread) shown.failure = null;
      this.unread = false;
    } catch (error) {
      if (!current()) return;
      // Why a version did not open stays said: a read does not speak over it.
      if (shown.failure === null || this.unread) {
        shown.failure = reason(error);
        this.unread = true;
      }
    }
    this.changed();
  }

  /**
   * Hand version *id* of the file shown to the user to save, one at a time. A version the pane
   * offers nothing on is refused: one it does not show, one no longer kept, and a deletion, which
   * left nothing to open. Why one could not be opened is said in the History, which is read again:
   * a version pruned since is then listed as no longer kept.
   */
  async openVersion(id: unknown): Promise<void> {
    const { project, shown } = this;
    const version = shown?.versions?.find((found) => found.id === id);
    if (!project || !shown || !version || !version.available || version.change === "deleted") {
      throw new Error("No such version in the History shown");
    }
    if (shown.opening !== null) throw new Error("A version of this file is already on its way");
    shown.opening = version.id;
    shown.failure = null;
    this.unread = false;
    this.changed();
    let failure: string | null = null;
    try {
      await this.projects.openVersion(project, { versionId: version.id, path: shown.path });
    } catch (error) {
      // One the page did not hand over in its time is not stopped by that: it is saved if it comes.
      failure = error instanceof TimedOut
        ? "The agent's page did not hand the version over in time: it may still be on its way, to be saved when it comes"
        : reason(error);
    }
    shown.opening = null;
    if (this.shown !== shown) return;
    shown.failure = failure;
    if (failure === null) this.changed();
    else await this.read();
  }
}

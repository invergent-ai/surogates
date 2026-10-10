// A file's History in the Overview's Library (spec, Section 13): its versions, who made each and
// when, with Open version and Restore, through the page's projects source. What it shows is the main
// process's, drawn by the window's page; what the page asks of it is checked against what it shows.

import type { FileVersion, ProjectsSource, UndoResult } from "../../../web/src/lib/projects-contract.js";
import { TimedOut } from "./projects.js";

export interface ShownHistory {
  path: string;
  versions: FileVersion[] | null; // null until it is read
  failure: string | null; // why the last thing asked of it did not happen, in the agent's words
  opening: string | null; // the version on its way to be saved: no other is opened meanwhile
  restoring: string | null; // the version on its way back as the file: no other is restored meanwhile
  result: UndoResult | null; // what the last Restore did, said until the next thing is asked of it
}

// A Restore the page did not answer in its two minutes: the agent goes on with it all the same.
const MAY_STILL_FINISH = "The agent's page did not answer in time: the restore may still finish, and this History shows it once it does";

type Source = Pick<ProjectsSource, "history" | "openVersion" | "restore">;

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
    this.shown = { path, versions: null, failure: null, opening: null, restoring: null, result: null };
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
      // Why a version did not open, or was not restored, stays said: a read does not speak over it.
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
    const { project, shown, version } = this.offered(id);
    if (shown.opening !== null) throw new Error("A version of this file is already on its way");
    shown.opening = version.id;
    shown.failure = null;
    shown.result = null;
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

  /**
   * Make the file shown version *id* again, as a landing by you, one at a time: the agent records
   * your edit to the file first. A version the pane offers no Restore on is refused, as Open version
   * refuses one. What it did, or why it did not, is said in the History, which is read again: once
   * it landed, its newest version is the restore, by you.
   */
  async restore(id: unknown): Promise<void> {
    const { project, shown, version } = this.offered(id);
    if (shown.restoring !== null) throw new Error("A version of this file is already being restored");
    shown.restoring = version.id;
    shown.failure = null;
    shown.result = null;
    this.unread = false;
    this.changed();
    try {
      const result = await this.projects.restore(project, { versionId: version.id, path: shown.path });
      if (this.shown === shown) shown.result = result;
    } catch (error) {
      if (this.shown === shown) shown.failure = error instanceof TimedOut ? MAY_STILL_FINISH : reason(error);
    }
    shown.restoring = null;
    if (this.shown === shown) await this.read();
  }

  // The version *id* of the file shown, where the pane offers something to do with it: none no longer kept, and no deletion.
  private offered(id: unknown): { project: string; shown: ShownHistory; version: FileVersion } {
    const { project, shown } = this;
    const version = shown?.versions?.find((found) => found.id === id);
    if (!project || !shown || !version || !version.available || version.change === "deleted") {
      throw new Error("No such version in the History shown");
    }
    return { project, shown, version };
  }
}

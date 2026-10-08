// The tool layer under a device's binder once the agent has a browser here (spec, Section 5):
// the browser's kinds go to the identity's browser host, every other kind to the tools beneath.

import { realpath, rm } from "node:fs/promises";
import { sep } from "node:path";

import type { FolderGuards } from "../binding/folder.js";
import { FOLDER_UNAVAILABLE } from "../hosts/messages.js";
import type { Operation, Outcome } from "../link/protocol.js";
import type { ToolLayer } from "../shell/device-stack.js";
import { type BrowserClient, PAUSED } from "./client.js";
import { interrupted, type StagedDownload, UNSAVED } from "./downloads.js";
import type { Launch } from "./host.js";

export const BROWSER_KINDS = "browser.";

// As surogates/devices/browser.py's NO_BROWSER says it, which the tools' result is.
export const NO_BROWSER: Outcome = {
  error: {
    type: "no_browser",
    message: "No supported browser on this computer. Install Google Chrome, Microsoft Edge, Brave or Vivaldi, or pick one in Settings → Browser. The Snap build of Chromium is not supported.",
  },
};

export const isBrowserKind = (kind: string): boolean => kind.startsWith(BROWSER_KINDS);

// What a session's downloads came to, at most this many to an answer, as the host's own notices.
const MAX_NOTICES = 20;

export interface BrowsingOptions {
  tools: ToolLayer;
  browser: Pick<BrowserClient, "perform" | "forget" | "stop" | "end" | "address" | "pause" | "show" | "onDownload">;
  // A browser operation runs only for a chat this computer bound: its binding, or undefined.
  bindingOf(root: string): unknown;
  // The browser Settings chose and the identity's profile for it, read at each operation; null: none here.
  launch(): Launch | null;
  // Where its browser host stages downloads: the host's own temporary folder, under the identity's profiles.
  // A staged file is read, and removed, only there.
  staging: string;
}

export class Browsing implements ToolLayer {
  // ponytail: the chat whose user holds the agent's browser, until that chat hands it back, in this run of the
  // app only: the browser ends with the app too, and its next launch is a new one. The browser is one for
  // every chat of the agent's here, every tab a tab of one window on one profile: so while it is held,
  // every chat's browser operations are answered paused, not only that chat's.
  private held: string | null = null;
  // Whether the chat it is held from was deleted since.
  private deleted = false;
  // What saves each download its browser stages, once the stack has said; and what each calling
  // session's downloads came to, with the chat it is of, until its next answer that says what its page did.
  private save: ((download: StagedDownload, stop: AbortSignal) => Promise<string>) | null = null;
  private readonly told = new Map<string, { root: string; notices: string[] }>();
  // ponytail: what stops each chat's saves, one for a chat that had a download, until the chat is deleted.
  private readonly saving = new Map<string, AbortController>();
  // The look at where each staged file is, one after another: downloads are saved in the order they were staged.
  private looking: Promise<unknown> = Promise.resolve();

  constructor(private readonly options: BrowsingOptions) {
    options.browser.onDownload((download) => {
      // This side knows that its user took the browser over before its browser host does.
      const held = this.held !== null;
      const looked = this.looking.then(() => this.staged(download.path));
      this.looking = looked;
      void looked.then((path) => this.saved(download, path, held)).catch(() => {});
    });
  }

  // The browser held by its user, or no browser here: refused before any chat's user is asked anything.
  refusal(operation: Operation): Outcome | null {
    if (!isBrowserKind(operation.kind)) return this.options.tools.refusal?.(operation) ?? null;
    if (this.held !== null) return PAUSED;
    return this.options.launch() === null ? NO_BROWSER : null;
  }

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (!isBrowserKind(operation.kind)) return this.options.tools.run(operation, signal);
    if (!this.options.bindingOf(operation.sessionId)) return Promise.resolve(FOLDER_UNAVAILABLE);
    // Let through before its user took the browser over, it never reaches the browser after.
    if (this.held !== null) return Promise.resolve(PAUSED);
    const launch = this.options.launch();
    if (!launch) return Promise.resolve(NO_BROWSER);
    return this.options.browser.perform(launch, operation, signal).then((outcome) => this.tell(operation.callingSessionId, outcome));
  }

  /**
   * What saves each download its browser stages: the stack's, which asks the chat's approvals and writes
   * through its file host. It is told to stop a chat's saves, by their signal, when the chat is deleted.
   */
  saveDownloadsWith(save: (download: StagedDownload, stop: AbortSignal) => Promise<string>): void {
    this.save = save;
  }

  // *path*: where the staged file really is, under the folder its browser host stages in; null for any other.
  // *held*: whether its user held the browser when the host handed it on.
  private async saved(download: StagedDownload, path: string | null, held: boolean): Promise<void> {
    // No file its browser host staged: nothing is read or removed on the host's word alone.
    if (path === null) return this.hear(download, UNSAVED);
    // The agent's own, handed on in the instant its user took the browser over, before the host heard of
    // it: dropped as one the host still had, and its agent told the same.
    if (held && !download.user) {
      await rm(path, { force: true }).catch(() => {});
      return this.hear(download, interrupted(download.name));
    }
    if (!this.save) {
      await rm(path, { force: true }).catch(() => {});
      return;
    }
    const stop = this.saving.get(download.root) ?? new AbortController();
    this.saving.set(download.root, stop);
    let notice: string;
    try {
      notice = await this.save({ ...download, path }, stop.signal);
    } catch {
      // What saves them says itself what came of each, and removes what was staged: of one it failed on
      // outright there is nothing to tell, and its file goes here.
      await rm(path, { force: true }).catch(() => {});
      return;
    }
    this.hear(download, notice);
  }

  // *path* by its real path, where that is under the folder its browser host stages downloads in; null for
  // any other: a file elsewhere, a link that leads out of the folder, nothing there, or no path at all. Never rejects.
  private async staged(path: string): Promise<string | null> {
    try {
      const [real, within] = await Promise.all([realpath(path), realpath(this.options.staging)]);
      return real.startsWith(`${within}${sep}`) ? real : null;
    } catch {
      return null;
    }
  }

  // What came of *download*, for its session's next answer that says what its page did.
  private hear(download: StagedDownload, notice: string): void {
    // One its user started while they held the browser is theirs: its agent hears nothing of it. Nor does a
    // chat that is gone: no answer of its sessions is left to carry it.
    if (download.user || !this.options.bindingOf(download.root)) return;
    const kept = this.told.get(download.session) ?? { root: download.root, notices: [] };
    if (kept.notices.length < MAX_NOTICES) kept.notices.push(notice);
    this.told.set(download.session, kept);
  }

  // *outcome*, with what the session's downloads came to when it says what the page did (its notices).
  // One answered paused says nothing of them: they stay for the session's next answer that does.
  private tell(session: string, outcome: Outcome): Outcome {
    const told = this.told.get(session)?.notices;
    const ok = "ok" in outcome ? outcome.ok : undefined;
    if (!told || typeof ok !== "object" || ok === null || !Array.isArray((ok as { notices?: unknown }).notices)) return outcome;
    this.told.delete(session);
    return { ok: { ...ok, notices: [...(ok as { notices: unknown[] }).notices, ...told] } };
  }

  address(session: string): Promise<string> {
    return this.options.browser.address(session);
  }

  // Whether the browser is held from a chat that is gone: deleted, or its folder forgotten on this computer.
  // That chat can hand nothing back, so the browser is nobody's to hand back but any chat's.
  private orphaned(): boolean {
    return this.held !== null && (this.deleted || !this.options.bindingOf(this.held));
  }

  /**
   * The chat's user takes the agent's browser over: every chat's browser operations are answered
   * paused_by_user until it is handed back. Whether the chat holds it now: another chat's take-over
   * does not take it from a chat that holds it, only from one that is gone.
   */
  takeOver(root: string): boolean {
    if (this.held === null || (this.held !== root && this.orphaned())) {
      this.held = root;
      this.deleted = false;
      this.options.browser.pause(root, true);
    }
    return this.held === root;
  }

  /**
   * The browser handed back, through the desktop's own confirmation: by the chat that holds it, or by
   * any chat once the one it was held from is gone. Another chat hands nothing back while its holder is
   * here. Whether it was handed back: who holds it can change while the confirmation is up.
   */
  handBack(root: string): boolean {
    const holder = this.held;
    if (holder === null || (holder !== root && !this.orphaned())) return false;
    this.held = null;
    this.options.browser.pause(holder, false);
    return true;
  }

  /**
   * Where the browser is held, as the chat is told: true from this chat; false by nobody, the agent drives
   * it; "elsewhere" from another chat that is here, which alone hands it back; "orphaned" from a chat that
   * is gone, which any chat may hand back.
   */
  takenOver(root: string): boolean | "orphaned" | "elsewhere" {
    if (this.held === null) return false;
    if (this.held === root) return true;
    return this.orphaned() ? "orphaned" : "elsewhere";
  }

  /** The chat's newest page brought to the front: whether there was one. */
  show(root: string): Promise<boolean> {
    return this.options.browser.show(root);
  }

  // A deleted chat: its tabs close, with every popup its sessions opened. A browser held from it stays
  // held: a page can have a chat deleted, and only the desktop's own confirmation hands the browser back.
  retired(root: string): void {
    if (this.held === root) this.deleted = true;
    // What its sessions' downloads came to is told to nobody now, and what is still being saved for it,
    // or asked about, stops.
    for (const [session, kept] of this.told) {
      if (kept.root === root) this.told.delete(session);
    }
    this.saving.get(root)?.abort();
    this.saving.delete(root);
    this.options.browser.forget(root);
    this.options.tools.retired?.(root);
  }

  guards(): FolderGuards {
    return this.options.tools.guards();
  }

  // A tab runs nothing of a chat's once its operation answers: what is alive is the tools'.
  live(): string[] {
    return this.options.tools.live();
  }

  // The app's quit, a log out: the browser closes, and the tools stop.
  async stop(): Promise<void> {
    await Promise.all([this.options.browser.stop(), this.options.tools.stop()]);
  }

  // The computer's access ended: the browser closes with every tab, and what runs in the tools ends.
  async end(): Promise<void> {
    await Promise.all([this.options.browser.end(), this.options.tools.end?.()]);
  }
}

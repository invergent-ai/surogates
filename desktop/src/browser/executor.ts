// The tool layer under a device's binder once the agent has a browser here (spec, Section 5):
// the browser's kinds go to the identity's browser host, every other kind to the tools beneath.

import { randomBytes } from "node:crypto";
import { realpath, rm } from "node:fs/promises";
import { basename, extname, sep } from "node:path";

import type { FolderGuards } from "../binding/folder.js";
import { MAX_WRITE_BYTES } from "../files/answers.js";
import { FOLDER_UNAVAILABLE } from "../hosts/messages.js";
import type { Operation, Outcome } from "../link/protocol.js";
import type { ToolLayer } from "../shell/device-stack.js";
import type { VmClient } from "../vm/client.js";
import { type BrowserClient, type Launch, PAUSED } from "./client.js";
import { interrupted, LEFT_TO_USER, type StagedDownload, UNSAVED } from "./downloads.js";

export const BROWSER_KINDS = "browser.";

// As surogates/devices/browser.py's NO_BROWSER says it, which the tools' result is.
export const NO_BROWSER: Outcome = {
  error: {
    type: "no_browser",
    message: "No supported browser on this computer. Install Google Chrome, Microsoft Edge, Brave or Vivaldi, or pick one in Settings → Browser. The Snap build of Chromium is not supported.",
  },
};

export const isBrowserKind = (kind: string): boolean => kind.startsWith(BROWSER_KINDS);

// Whether *download* is its user's only because it came just after they handed the browser back.
const justAfter = (download: StagedDownload): boolean => download.user && download.afterHandBack === true;

// What a session's downloads came to, at most this many to an answer, as the host's own notices.
const MAX_NOTICES = 20;
// The most files one upload gives a page, as the tool's schema says (surogates/tools/builtin/browser.py).
const MAX_UPLOAD_FILES = 10;
// ponytail: the types pages most often check a file input's files for; any other is application/octet-stream.
const TYPES: Record<string, string> = {
  ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
  ".webp": "image/webp", ".svg": "image/svg+xml", ".txt": "text/plain", ".csv": "text/csv", ".json": "application/json",
  ".html": "text/html", ".zip": "application/zip", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".mp3": "audio/mpeg", ".mp4": "video/mp4",
};
const failed = (message: string): Outcome => ({ error: { type: "browser", message } });

// A control character, or a separator of lines or of paragraphs: in a path, it would let one file's name read
// as another's, or as two, wherever the path is shown.
const UNSHOWN = /[\p{Cc}\p{Zl}\p{Zp}]/u;
// Half of a character that is written as two: alone it is no character, and no file's name holds it. Shown,
// and sent on to be read, it becomes another character, so the path asked about names another file.
const HALVED = /\p{Cs}/u;
// Why an upload that names *paths* gives a page nothing, whatever its files hold; null where nothing in how
// it names them says so.
function unfit(paths: unknown): string | null {
  if (!Array.isArray(paths)) return null;
  for (const path of paths) {
    if (typeof path !== "string") continue;
    if (UNSHOWN.test(path)) return `An upload gives a page no file whose path holds a line break or another control character: ${JSON.stringify(path)}`;
    if (HALVED.test(path)) return `An upload gives a page no file whose path holds half a character, which no file's name can: ${JSON.stringify(path)}`;
    // The page is given a file by its last name alone. A backslash in that is a folder's separator elsewhere, and
    // the browser host takes no such name for a file's (host.ts, filesOf).
    if (basename(path).includes("\\")) {
      return `An upload gives a page no file whose name holds a backslash, which a page can take for a folder's: ${JSON.stringify(path)}`;
    }
  }
  return null;
}

export interface BrowsingOptions {
  tools: ToolLayer;
  browser: Pick<BrowserClient, "perform" | "forget" | "stop" | "end" | "address" | "notComing" | "pause" | "show" | "onDownload" | "forwards">;
  // A browser operation runs only for a chat this computer bound: its binding, or undefined.
  bindingOf(root: string): unknown;
  // The browser Settings chose and the identity's profile for it, read at each operation; null: none here.
  launch(): Launch | null;
  // Where its browser host stages downloads: the host's own temporary folder, under the identity's profiles.
  // A staged file is read, and removed, only there.
  staging: string;
  // The ports of chats' own servers their users let the browser open, each with its chat's root, as the journal holds them now.
  ports(): Array<{ port: number; root: string }>;
  // Every port that has turned, given to a chat, moved or taken back, with the mark of its last turn, as the journal holds them now.
  turns(): Array<{ port: number; turn: string }>;
  // The sandbox's side of them: told each port's root, asked whether a root listens on one, and where the browser's proxy knocks.
  vm: Pick<VmClient, "forwards" | "listening" | "door">;
}

export class Browsing implements ToolLayer {
  // ponytail: the chat whose user holds the agent's browser, until that chat hands it back, in this run of the
  // app only: the browser ends with the app too, and its next launch is a new one. The browser is one for
  // every chat of the agent's here, every tab a tab of one window on one profile: so while it is held,
  // every chat's browser operations are answered paused, not only that chat's.
  private held: string | null = null;
  // Whether the chat it is held from was deleted since.
  private deleted = false;
  // How many times the browser was taken over: what was begun before the last of them is not taken up again
  // at a hand back, though it only finds out after it.
  private takes = 0;
  // What saves each download its browser stages, once the stack has said; and what each calling
  // session's downloads came to, with the chat it is of, until its next answer that says what its page did.
  private save: ((download: StagedDownload, stop: AbortSignal) => Promise<string>) | null = null;
  private readonly told = new Map<string, { root: string; notices: string[] }>();
  // ponytail: what stops each chat's saves, one for a chat that had a download, until the chat is deleted.
  private readonly saving = new Map<string, AbortController>();
  // The look at where each staged file is, one after another: downloads are saved in the order they were staged.
  private looking: Promise<unknown> = Promise.resolve();
  // What this device's browser knocks with at the sandbox's door, for this run of the app: 256 bits no other
  // process is given but the browser host and the VM manager, so nothing else on this computer opens that door.
  private readonly key = randomBytes(32).toString("hex");
  // The ports told last, so a change of the bindings that changes none of them tells nobody.
  private toldPorts: string | null = null;
  // Stopped, or its computer's access ended: nothing is forwarded again in this run, whatever the journal holds.
  private over = false;

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
    if (this.options.launch() === null) return NO_BROWSER;
    // An upload that names a file as no prompt could show it truly is refused before anyone is asked about it.
    const why = operation.kind === "browser.set_input_files" ? unfit(operation.args.paths) : null;
    return why === null ? null : failed(why);
  }

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    if (!isBrowserKind(operation.kind)) return this.options.tools.run(operation, signal);
    if (!this.options.bindingOf(operation.sessionId)) return Promise.resolve(this.ended(operation, FOLDER_UNAVAILABLE));
    // Let through before its user took the browser over, it never reaches the browser after.
    if (this.held !== null) return Promise.resolve(this.ended(operation, PAUSED));
    const launch = this.options.launch();
    if (!launch) return Promise.resolve(this.ended(operation, NO_BROWSER));
    return this.browse(launch, operation, signal);
  }

  // *outcome*, for an operation that ends here, without reaching the browser. Where it is an upload, the
  // browser may keep an input for it since its user was asked about it: told that it is not coming.
  private ended(operation: Operation, outcome: Outcome): Outcome {
    if (operation.kind === "browser.set_input_files") this.options.browser.notComing(operation.id);
    return outcome;
  }

  private async browse(launch: Launch, operation: Operation, signal: AbortSignal): Promise<Outcome> {
    const begun = this.takes;
    const sent = operation.kind === "browser.set_input_files" ? await this.withFiles(operation, signal, begun) : operation;
    if (!("kind" in sent)) return this.ended(operation, sent);
    // Its user took the browser over while its files were read, handed back since or not: none of them leaves this process.
    if (this.takes !== begun) return this.ended(operation, PAUSED);
    const outcome = await this.options.browser.perform(launch, sent, signal);
    // An answer made for an operation that was cancelled meanwhile reaches no one: it takes with it nothing of
    // what the session's downloads came to, which its next answer says.
    return signal.aborted ? outcome : this.tell(operation.callingSessionId, outcome);
  }

  // An upload, with what each file it names holds, read through the chat's file host as any read of
  // its folder: the browser is given the files, never a path, and nothing outside the folder.
  // *begun*: how many times the browser had been taken over when the upload began.
  private async withFiles(operation: Operation, signal: AbortSignal, begun: number): Promise<Operation | Outcome> {
    const { paths } = operation.args;
    // The server refuses these already (surogates/tools/builtin/browser.py): this computer checks what it is sent all the same.
    if (!Array.isArray(paths) || paths.length === 0 || paths.length > MAX_UPLOAD_FILES || !paths.every((path) => typeof path === "string" && path !== "")) {
      return failed(`An upload names 1 to ${MAX_UPLOAD_FILES} files of the chat's folder`);
    }
    // Refused before anyone was asked (refusal); here for one that came unasked, as in a chat that works freely.
    const why = unfit(paths);
    if (why !== null) return failed(why);
    const files: Array<{ name: string; mimeType: string; buffer: string }> = [];
    let bytes = 0;
    for (const key of paths as string[]) {
      // Its user took the browser over meanwhile, handed back since or not: no more of them is read.
      if (this.takes !== begun) return PAUSED;
      const read = await this.options.tools.run({ ...operation, id: `${operation.id}:read-${files.length}`, kind: "read", args: { key, max_bytes: null } }, signal);
      if ("error" in read) return failed(`${key} could not be read for the page: ${read.error.message}`);
      // A read answers the file's data, in base64: anything else is no file to give.
      if (typeof read.ok !== "string") return failed(`${key} could not be read for the page: its file host answered no data`);
      const buffer = read.ok;
      bytes += Buffer.byteLength(buffer, "base64");
      if (bytes > MAX_WRITE_BYTES) return failed(`The files are too large to give the page at once: at most ${MAX_WRITE_BYTES} bytes`);
      files.push({ name: basename(key), mimeType: TYPES[extname(key).toLowerCase()] ?? "application/octet-stream", buffer });
    }
    return { ...operation, args: { files } };
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
    if (path === null) return this.hear(download, justAfter(download) ? LEFT_TO_USER : UNSAVED);
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
      // outright there is nothing to tell, and its file goes here. One that may be the agent's own was not saved.
      await rm(path, { force: true }).catch(() => {});
      if (justAfter(download)) this.hear(download, LEFT_TO_USER);
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
    // One that is its user's outright is theirs: its agent hears nothing of it, saved or not. One that is
    // theirs only because it came just after they handed the browser back may be the agent's own, which
    // hears what came of it. A chat that is gone hears nothing: no answer of its sessions is left to carry it.
    if ((download.user && !justAfter(download)) || !this.options.bindingOf(download.root)) return;
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

  address(session: string, upload?: boolean, of?: string, root?: string): Promise<string | { refused: string }> {
    return this.options.browser.address(session, upload, of, root);
  }

  /** An upload the browser was asked about, by its operation's id, got no leave: it is not coming. */
  notComing(of: string): void {
    this.options.browser.notComing(of);
  }

  /**
   * What the browser may open of its chats' own servers (spec, Section 5), told to the two that enforce it:
   * the sandbox, each port with its chat's root, and the browser's proxy, the ports alone. Called at the
   * start and at each change of the bindings: a port allowed, taken back, moved to another chat, or gone
   * with a deleted chat. The browser hears each port's last turn too, by which it clears what a page of a
   * port's origins stored before the port turned. A journal that cannot be read forwards nothing; nor does
   * a run that has stopped.
   */
  forwarded(): void {
    if (this.over) return;
    let ports: Array<{ port: number; root: string }> = [];
    let turns: Array<{ port: number; turn: string }> = [];
    try {
      [ports, turns] = [this.options.ports(), this.options.turns()];
    } catch {
      // Nothing is forwarded.
    }
    this.forward(ports, turns);
  }

  private forward(ports: Array<{ port: number; root: string }>, turns: Array<{ port: number; turn: string }>): void {
    const telling = JSON.stringify([ports.map(({ port, root }) => [port, root]), turns.map(({ port, turn }) => [port, turn])]);
    if (telling === this.toldPorts) return;
    this.toldPorts = telling;
    this.options.vm.forwards(this.key, ports.map(({ port, root }) => [port, root]));
    this.options.browser.forwards(ports.map(({ port }) => port), this.options.vm.door, this.key, turns.map(({ port, turn }) => [port, turn]));
  }

  /** Whether something in *root*'s sandbox listens on *port* of its own loopback now, or "busy" where it could not be asked: no sandbox is started to ask. */
  listening(root: string, port: number): Promise<boolean | "busy"> {
    return this.options.vm.listening(root, port);
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
      this.takes += 1;
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
   * Whether the browser is held from a chat that is gone. No pane of that chat is left to hand it back in,
   * and another chat has one only where its own browser is open: so the desktop offers it itself, in Settings.
   */
  heldFromGone(): boolean {
    return this.orphaned();
  }

  /**
   * The browser handed back by the desktop itself, through its own confirmation, where the chat it is held
   * from is gone. Whether it was: held from a chat that is here, it is that chat's to hand back, and nothing is.
   */
  handBackGone(): boolean {
    const holder = this.held;
    if (holder === null || !this.orphaned()) return false;
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

  keepsCopies(): boolean {
    return this.options.tools.keepsCopies?.() === true;
  }

  // A tab runs nothing of a chat's once its operation answers: what is alive is the tools'.
  live(): string[] {
    return this.options.tools.live();
  }

  // The app's quit, a log out: nothing is forwarded any more, the browser closes, and the tools stop. The
  // journal keeps the ports, as it keeps the bindings: the next start forwards them under another key.
  async stop(): Promise<void> {
    this.over = true;
    this.forward([], []);
    await Promise.all([this.options.browser.stop(), this.options.tools.stop()]);
  }

  // The computer's access ended: nothing is forwarded any more, the browser closes with every tab, and what runs in the tools ends.
  async end(): Promise<void> {
    this.over = true;
    this.forward([], []);
    await Promise.all([this.options.browser.end(), this.options.tools.end?.()]);
  }
}

// Folders outside a chat's folder (spec, Part A), done by the app itself: no sandbox binds them, so this layer is the
// containment. It answers outside.* under the binder, in the file helper's words (operations.ts), and hands every other
// kind to the tools below. A read is let through only in a folder its chat's user granted (approvals.ts asks for the
// grant), and only where no name on the way is a link (places.ts, withinGrant). That check holds nothing afterwards,
// so the read itself goes from the grant's folder one name at a time, each held without following a link: a link put
// on the way after the check leads the read nowhere. A file with two hard links is one file in both places, and no
// look tells which name is its own: one in a grant is read like any other. A copy is read the same way, from a grant
// or by the chat's folder's own tools, and only ever makes a new file: into the chat's folder by its own tools, as a
// download is saved (browser/downloads.ts), and into a grant by a create-only open at the end of the same walk down.
// A trash and a put back only ever touch a grant's files, never the chat's folder or one that holds it: the chat's own
// files are its tools' to delete. A put back sends the original to the bin only once the changes sit checked beside it,
// and gives them its name by a link, which fails where a name is taken, so no file made there meanwhile is replaced.

import { randomUUID } from "node:crypto";
import { type BigIntStats, closeSync, constants, fchmodSync, readSync, writeSync } from "node:fs";
import { extname, posix } from "node:path";

import { type Approvals, OUTSIDE, outsideDenied as denied, OUTSIDE_READS as READS, UNKNOWN as unknown } from "../binding/approvals.js";
import type { FolderGuards } from "../binding/folder.js";
import { Failure, FOLDER_UNAVAILABLE, fromNode, io, MAX_MESSAGE_CHARS, READ_TOO_LARGE, type Refusal } from "../files/answers.js";
import type { FdPaths, Where } from "../files/fd-paths.js";
import { FLAGS } from "../files/flags.js";
import { linesFrom, listed, readFrom, readFromAsync, revisionOf, statOf, walkFrom } from "../files/operations.js";
import { type Identity, identityOf, sameFile } from "../files/identity.js";
import { fileOs } from "../files/os.js";
import { inside } from "../files/paths.js";
import { explorerRefusal } from "../files/protect.js";
import type { Bindings } from "../journal/bindings.js";
import type { Operation, Outcome } from "../link/protocol.js";
import { BIN } from "../platform.js";
import { basename, hostPathOf, isAbsolute, join, resolve } from "../session-path.js";
import type { ToolLayer } from "../shell/device-stack.js";
import { lookApart } from "../shell/folders-held.js";
import { grantable, isKnownName, knownFolder, withinGrant } from "./places.js";

const noPermission = (path: string): Outcome => ({
  error: { type: "sandbox", message: `No permission to read ${path}: ask for it first with request_folder_access.` },
});
// A file the window listed that its user left unticked: said, so the agent never takes it for done.
const UNTICKED = "not ticked by the user";
const byAnotherName = (path: string) => `${path} is in this chat's folder by another name: use its path in the chat's folder.`;
const chatsOwn = (path: string): Outcome => denied(`${path} is this chat's folder, in it, or holds it: the chat's own files are deleted with its own tools.`);
const notDestination = (to: string): Outcome => denied(`The destination ${to} is neither this chat's folder nor a folder it may read.`);
// How many numbered names a copy tries past its own, as a download does.
const NAMES = 100;
const TOO_LARGE = "too large";
// Why a file was not copied, as a copy's or a move's result says it.
const skippedFor = (error: unknown): string => {
  const reason = said(error);
  return reason === READ_TOO_LARGE ? TOO_LARGE : reason;
};
const said = (error: unknown) => error instanceof Failure ? error.refusal.message : error instanceof Error ? error.message : String(error);
const osFailure = (message: string) => new Failure({ type: "os", message });
// An outcome's refusal, thrown: for one known to be refused.
const failureOf = (outcome: Outcome) => new Failure((outcome as { error: Refusal }).error);
// A file operation of the chat's folder's own tools, run as one of an outside kind's steps.
type Inner = (kind: string, args: Record<string, unknown>) => Promise<Outcome>;
// The grant of a chat's that holds a path, no link on its way, or null (Outside.looker): each path looked at once a call.
type Look = (path: string) => string | null;
// An outside walk's bounds: shorter than a walk of the chat's folder, as it runs on the app's main thread (see Outside).
const OUTSIDE_WALK_MS = 1_000;
const OUTSIDE_WALK_FILES = 2_000;
// A put back's temporary name beside the original: short and of one length, so an original whose name is near the
// OS's longest still has one.
const temporaryName = () => `.sgpb-${randomUUID().slice(0, 8)}`;
// What a create-only open left behind: whether it opened, and the identity of the file it made, once read.
type Made = { opened: boolean; made: BigIntStats | null };
/**
 * Why the bin refused a file, in words its user can act on. Electron's shell.trashItem says it in an Error of its own
 * words, with no code: on Windows, which refuses to move a file open in another program, that may be why; elsewhere an
 * open file never stops a move, so the bin's words are said alone.
 */
export function binError(error: unknown, platform: NodeJS.Platform = process.platform): Failure {
  return osFailure(platform === "win32" ? `${said(error)} (it may be open in another program)` : said(error));
}

/** The *n*th name a copy of *name* tries: its own first, then "name (1).ext", "name (2).ext", and on. */
export function numberedName(name: string, n: number): string {
  if (n === 0) return name;
  const extension = extname(name);
  return `${name.slice(0, name.length - extension.length)} (${n})${extension}`;
}

// The files *requested* named that its user left unticked, once each.
const unticked = (requested: unknown, ticked: string[]): string[] =>
  Array.isArray(requested) ? [...new Set(requested.filter((file): file is string => typeof file === "string" && !ticked.includes(file)))] : [];

// What a copy read: the file's bytes, base64, and, for one from a grant, its revision as read.
type Source = { data: string; revision?: string };
type Copied = { copied: Array<{ from: string; to: string }>; skipped: Array<{ from: string; reason: string }> };
type Moved = {
  moved: Array<{ from: string; to: string }>;
  copied_only: Array<{ from: string; to: string; reason: string }>; // copied, but its original stayed where it is
  skipped: Array<{ from: string; reason: string }>;
};

export interface OutsideOptions {
  tools: ToolLayer;
  bindings: Bindings;
  approvals: Pick<Approvals, "chosenFor">;
  fd: FdPaths;
  getPath: (name: "desktop" | "downloads" | "documents" | "pictures") => string;
  /** The file at this computer's own *hostPath* moved to its BIN (Electron's shell.trashItem); rejects if not. */
  trash: (hostPath: string) => Promise<void>;
  /** A look at the folder at the session's path *folder* off the main thread, within a bound (lookApart by default). */
  look?: (folder: string) => Promise<"there" | "gone" | "silent">;
  /**
   * Chat *root*'s turns followed: *turnEnded* each time one ends, until the stop it returns; undefined where they cannot
   * be followed (nobody signed in), and then a grant for one turn lasts until the app restarts.
   */
  following?: (root: string, turnEnded: () => void) => (() => void) | undefined;
}

// Every other member of the tools below, there only where they have it: a caller tells one they lack by its absence.
const PASSED = [
  "retired", "keepsCopies", "folderInUse", "address", "notComing", "takeOver", "handBack", "takenOver", "heldFromGone", "handBackGone",
  "show", "saveDownloadsWith", "forwarded", "listening", "admit", "acknowledged", "end",
] as const satisfies ReadonlyArray<keyof ToolLayer>;
type Passed = (typeof PASSED)[number];
// A member ToolLayer gains is passed down, or this fails to compile.
const EVERY: [Exclude<keyof ToolLayer, Passed | "run" | "refusal" | "resolveFolder" | "guards" | "live" | "stop">] extends [never] ? true : never = true;
void EVERY;

// The members the constructor assigns from PASSED.
export interface Outside extends Pick<ToolLayer, Passed> {}

// ponytail: every outside read, walk, copy and put back runs in the main process, a stale or slow drive's wait on its
// thread (a grant's folder is looked at apart first, a walk has a short budget, a read yields between its pieces); the
// upgrade is to move this layer's I/O into the file host's process, as a chat's folder's is.
export class Outside implements ToolLayer {
  // Each chat with grants for its turn alone, followed until that turn ends (heldForTurn), by the stop of its following,
  // and how many such grants it had when last told.
  private readonly turns = new Map<string, () => void>();
  private readonly turnGrants = new Map<string, number>();

  constructor(private readonly options: OutsideOptions) {
    const { tools } = options;
    for (const name of PASSED) {
      const member = tools[name] as ((...args: never[]) => unknown) | undefined;
      if (member) Object.assign(this, { [name]: member.bind(tools) });
    }
    options.bindings.watch((root) => this.heldForTurn(root));
  }

  // A chat's turn followed from the moment a grant for it alone is made, whoever made it and whether or not its
  // operation runs: followed anew at each such grant, so a follow that stopped by itself (a sign-out) is started again;
  // and let go once it has none, its turn over, taken back, or the chat removed.
  private heldForTurn(root: string): void {
    const { bindings } = this.options;
    const held = bindings.reads(root).filter((folder) => bindings.forTurn(root, folder)).length;
    const before = this.turnGrants.get(root) ?? 0;
    if (held === before) return;
    if (held === 0) this.turnGrants.delete(root);
    else this.turnGrants.set(root, held);
    if (held > 0 && held < before) return;
    this.turns.get(root)?.();
    this.turns.delete(root);
    if (held === 0) return;
    const stop = this.options.following?.(root, () => bindings.endTurnReads(root));
    if (stop) this.turns.set(root, stop);
  }

  /**
   * The folder an outside.grant names, as its window shows it and its grant keeps it: a known folder ("Desktop") where
   * the OS keeps it, or an absolute path, as it resolves; or why the chat *root* may not be granted it.
   */
  async resolveFolder(folder: string, root: string): Promise<{ folder: string } | { refused: string }> {
    const binding = this.options.bindings.get(root);
    if (!binding) return { refused: failureOf(FOLDER_UNAVAILABLE).refusal.message };
    const known = isKnownName(folder);
    const path = known ? knownFolder(folder, this.options.getPath) : isAbsolute(folder) ? folder : null;
    if (path === null) {
      return {
        refused: known
          ? `Surogate cannot read ${folder}: this computer keeps it on no drive of its own.`
          : `'${folder}' is not a folder Surogate can ask for: name Desktop, Downloads, Documents or Pictures, or give an absolute path.`,
      };
    }
    // Looked at apart first: the checks below resolve it on the main thread, which a drive that has stopped answering
    // (a network share, a mount gone stale) would stop for as long. One gone is the checks' to say so.
    const look = this.options.look ?? ((at: string) => lookApart(hostPathOf(at)));
    if ((await look(path)) === "silent") {
      return { refused: `Surogate cannot read ${folder}: it did not answer in time, as a network drive or one that has stopped answering may not.` };
    }
    const granted = grantable(path, this.options.tools.guards(), binding.folder);
    return granted.ok ? { folder: granted.folder } : { refused: granted.message };
  }

  refusal(operation: Operation): Outcome | null {
    const { kind, args, sessionId: root } = operation;
    if (!kind.startsWith(OUTSIDE)) return this.options.tools.refusal?.(operation) ?? null;
    // An unbound chat's, and a project thread's, the approvals answer in their own words.
    const binding = this.options.bindings.get(root);
    if (!binding || binding.history !== undefined) return null;
    if (kind === "outside.grant") {
      // Its folder is resolved, and checked, by the approvals before its user is asked and by run(), each once it was
      // looked at apart: here, on the main thread before either, it is not touched.
      return typeof args.folder === "string" ? null : denied("'folder' must be a folder's name or an absolute path.");
    }
    const look = this.looker(root);
    if (READS.has(kind)) {
      const path = pathOf(operation);
      if (typeof path !== "string") return path;
      return look(path) === null ? noPermission(path) : null;
    }
    if (kind === "outside.trash" && Array.isArray(args.files)) {
      const own = args.files.find((file) => typeof file === "string" && this.chatOwns(root, file, look)) as string | undefined;
      if (own !== undefined) return chatsOwn(own);
    }
    if (kind === "outside.move") {
      // A file in the chat's folder may be moved out, by its path there; never the folder itself, one that holds it, or
      // the chat's folder by another name. And as copy's, no destination by another name.
      const own = (Array.isArray(args.files) ? args.files : []).find((file) => typeof file === "string" && this.holdsChat(root, file, look)) as string | undefined;
      if (own !== undefined) return chatsOwn(own);
      if (typeof args.to === "string" && this.reachesChat(root, args.to, look)) return denied(byAnotherName(args.to));
    }
    if (kind === "outside.put_back") return typeof args.copy === "string" && typeof args.original === "string" ? this.putBackRefusal(root, args.copy, args.original, look) : null;
    if (kind === "outside.copy") {
      // A path to the chat's folder by another name is no grant's, and the chat's own tools know it by its own.
      const named = [...(Array.isArray(args.files) ? args.files : []), args.to].find((path) => typeof path === "string" && this.reachesChat(root, path, look));
      if (typeof named === "string") return denied(byAnotherName(named));
    }
    if ((kind === "outside.copy" || kind === "outside.trash" || kind === "outside.move") && Array.isArray(args.files)) {
      // Malformed ones are the approvals' to refuse, in their words. A copy may take from the chat's folder too.
      const out = args.files.find((file) => typeof file === "string" && this.sourceOf(root, file, look, kind !== "outside.trash") === null) as string | undefined;
      if (out !== undefined) return noPermission(out);
    }
    if ((kind === "outside.copy" || kind === "outside.move") && typeof args.to === "string" && this.destinationOf(root, args.to, look) === null) return notDestination(args.to);
    return null;
  }

  async run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    const { kind, args, sessionId: root } = operation;
    if (!kind.startsWith(OUTSIDE)) return this.options.tools.run(operation, signal);
    if (!this.options.bindings.get(root)) return FOLDER_UNAVAILABLE;
    if (kind === "outside.grant") {
      // Kept by the approvals on its user's allow: answered with where, once it is kept.
      if (typeof args.folder !== "string") return denied("'folder' must be a folder's name or an absolute path.");
      // The folder its user allowed, as the approvals kept it: answered so, whatever its name resolves to now (a
      // Desktop the OS moved since), as long as it is still this chat's grant.
      const approved = this.options.approvals.chosenFor(operation.id)?.[0];
      if (approved !== undefined && this.options.bindings.reads(root).includes(approved)) return { ok: this.granted(root, approved, approved) };
      const resolved = await this.resolveFolder(args.folder, root);
      if ("refused" in resolved) return denied(resolved.refused);
      // Answered as granted where a grant of this chat's holds it, as the approvals let it through unasked.
      const holder = this.options.bindings.reads(root).includes(resolved.folder) ? resolved.folder : this.looker(root)(resolved.folder);
      return holder !== null ? { ok: this.granted(root, resolved.folder, holder) } : noPermission(resolved.folder);
    }
    if (kind === "outside.copy") return this.copy(operation, signal);
    if (kind === "outside.trash") return this.trash(operation, signal);
    if (kind === "outside.move") return this.move(operation, signal);
    if (kind === "outside.put_back") return this.putBack(operation, signal);
    if (!READS.has(kind)) return unknown(kind);
    // Checked again: an operation reaches here without its refusal asked, as one taken up again after a restart.
    const key = pathOf(operation);
    if (typeof key !== "string") return key;
    const grant = this.looker(root)(key);
    if (grant === null) return noPermission(key);
    return this.answer(kind, () => this.read(kind, args, grant, key));
  }

  // *folder* granted to *root* by its grant *holder*, and for how long: a grant for the turn ends with it (heldForTurn).
  private granted(root: string, folder: string, holder: string): { folder: string; lasts: "this request" | "this chat" } {
    return { folder, lasts: this.options.bindings.forTurn(root, holder) ? "this request" : "this chat" };
  }

  // For one refusal() or run(): the grant of *root*'s that holds a path, no link on its way; null for none. Each path
  // is looked at once a call, against the grants as they are then. On Windows a folder is looked at only once the file
  // host bound it: each grant is bound here, once a call, which keeps its folder held for the app's life, and does
  // nothing for one bound already. A grant taken back is still bound, and is in no list of grants, so it holds nothing.
  private looker(root: string): Look {
    const { fd } = this.options;
    const found = new Map<string, string | null>();
    const bound = new Set<string>();
    return (path) => {
      const known = found.get(path);
      if (known !== undefined) return known;
      const grants = this.options.bindings.reads(root);
      for (const grant of grants) {
        if (bound.has(grant)) continue;
        bound.add(grant);
        try {
          fd.bind([grant]);
        } catch {
          // Gone, or no longer where its path leads: withinGrant finds nothing in it.
        }
      }
      const grant = withinGrant(grants, path, fd);
      found.set(path, grant);
      return grant;
    };
  }

  // Where a copy's *file* is read from: the chat's folder ("chat"), for a file in it, or the grant that holds it; null
  // for neither. The chat's folder is no grant's: a file there is the chat's own, read by its own tools.
  private sourceOf(root: string, file: string, look: Look, chat = true): "chat" | string | null {
    const folder = this.options.bindings.get(root)?.folder;
    if (chat && folder !== undefined && file !== folder && inside(file, folder)) return "chat";
    return this.reachesChat(root, file, look) ? null : look(file);
  }

  // Where a copy may go, as the folder it goes in: the chat's folder or one in it (grant null), written by its own
  // tools even where a grant holds the chat's folder too; or a folder in a grant, by that grant. Null for anywhere else.
  private destinationOf(root: string, to: string, look: Look): { dir: string; grant: string | null } | null {
    const folder = this.options.bindings.get(root)?.folder;
    if (folder !== undefined && (to === "." || inside(to, folder))) return { dir: to === "." ? folder : to, grant: null };
    const grant = look(to);
    return grant === null || this.reachesChat(root, to, look) ? null : { dir: to, grant };
  }

  // *operation*'s own steps by the chat's folder's tools, each an operation of its own, named *prefix* and a new id.
  private inner(operation: Operation, prefix: string, signal: AbortSignal): Inner {
    return (kind, args) => this.options.tools.run({ ...operation, id: `${prefix}-${randomUUID()}`, kind, args, digest: "" }, signal);
  }

  // The files its user ticked, each copied on its own: one that cannot be is said, and the rest are copied all the same.
  private async copy(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    const { args, sessionId: root } = operation;
    // Let go before anything else: a copy refused here is not done later with them.
    const ticked = this.options.approvals.chosenFor(operation.id);
    if (typeof args.to !== "string") return { error: { type: "value", message: "'to' must be a string" } };
    // Checked again: an operation reaches here without its refusal asked, as one taken up again after a restart.
    const look = this.looker(root);
    if (this.reachesChat(root, args.to, look)) return denied(byAnotherName(args.to));
    const to = this.destinationOf(root, args.to, look);
    if (to === null) return notDestination(args.to);
    // Taken up again after a restart, its user's ticks are gone: nothing is copied that nobody chose.
    if (ticked === undefined) return denied("Nothing was copied: the files its user chose are not known any more. Ask again.");
    const done: Copied = { copied: [], skipped: [] };
    const inner = this.inner(operation, "outside-copy", signal);
    for (const from of ticked) {
      if (signal.aborted) {
        done.skipped.push({ from, reason: "stopped" });
        continue;
      }
      try {
        done.copied.push({ from, to: (await this.copyOne(root, from, to, look, inner)).key });
      } catch (error) {
        done.skipped.push({ from, reason: skippedFor(error) });
      }
    }
    for (const from of unticked(args.files, ticked)) done.skipped.push({ from, reason: UNTICKED });
    return { ok: done };
  }

  // *from* copied into *to*: the new file's key, and the revision of an outside original as it was read. Or throws.
  private async copyOne(root: string, from: string, to: { dir: string; grant: string | null }, look: Look, inner: Inner): Promise<{ key: string; revision?: string }> {
    if (this.reachesChat(root, from, look)) throw new Failure({ type: "denied", message: byAnotherName(from) });
    const source = this.sourceOf(root, from, look);
    if (source === null) throw failureOf(noPermission(from));
    let read: Source;
    if (source === "chat") {
      const outcome = await inner("read", { key: from, max_bytes: null });
      if ("error" in outcome) throw new Failure(outcome.error);
      read = { data: outcome.ok as string };
    } else {
      read = this.within(root, source, from, (where) => ({
        // Looked at before the read: a change in between makes the revision older than the bytes, and a put back
        // then finds the original changed, and refuses; never the other way round.
        revision: revisionOf(io(from, () => this.options.fd.lstat(where))),
        data: readFrom({ max_bytes: null }, from, (flags) => this.options.fd.open(where, flags)),
      }));
    }
    const name = basename(from);
    const key = to.grant === null ? await this.intoChat(to.dir, name, read.data, inner) : this.intoGrant(root, to.grant, to.dir, name, read.data);
    // A copy of an outside file is remembered for its put back; any other made at a key once a copy's is no copy.
    if (to.grant === null) {
      if (source === "chat") this.options.bindings.forgetCopy(root, key);
      else this.options.bindings.recordCopy(root, key, from, read.revision!);
    }
    return { key, revision: read.revision };
  }

  // The files its user ticked, each copied to *to* and, once its copy is made and checked, its original to the bin, or,
  // for a file of the chat's own folder moved out of it, deleted there: one that cannot be copied stays where it is, and
  // one whose original cannot go (changed since it was read, say) is said with its copy.
  private async move(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    const { args, sessionId: root } = operation;
    // Let go before anything else: a move refused here is not done later with them.
    const ticked = this.options.approvals.chosenFor(operation.id);
    if (typeof args.to !== "string") return { error: { type: "value", message: "'to' must be a string" } };
    // Checked again: an operation reaches here without its refusal asked, as one taken up again after a restart.
    const look = this.looker(root);
    if (this.reachesChat(root, args.to, look)) return denied(byAnotherName(args.to));
    const to = this.destinationOf(root, args.to, look);
    if (to === null) return notDestination(args.to);
    if (ticked === undefined) return denied("Nothing was moved: the files its user chose are not known any more. Ask again.");
    const done: Moved = { moved: [], copied_only: [], skipped: [] };
    const inner = this.inner(operation, "outside-move", signal);
    for (const from of ticked) {
      if (signal.aborted) {
        done.skipped.push({ from, reason: "stopped" });
        continue;
      }
      let copied: { key: string; revision?: string };
      // A file of the chat's own folder, moved out of it: copied, then deleted there by its own tools, as a working copy put back is.
      let own = false;
      try {
        if (this.holdsChat(root, from, look)) throw failureOf(chatsOwn(from));
        own = this.sourceOf(root, from, look) === "chat";
        // Moved into its own folder it would be only renamed "name (1)": it is where it is to go already.
        if (posix.dirname(from) === to.dir) throw osFailure("it is in that folder already");
        if (own && to.grant === null) throw osFailure("it is in this chat's folder already: move it there with the chat's own tools");
        copied = await this.copyOne(root, from, to, look, inner);
      } catch (error) {
        done.skipped.push({ from, reason: skippedFor(error) });
        continue;
      }
      try {
        if (own) {
          const deleted = await inner("delete", { key: from });
          if ("error" in deleted) throw new Failure(deleted.error);
        } else {
          await this.trashOne(root, from, this.looker(root), copied.revision, signal);
        }
      } catch (error) {
        done.copied_only.push({ from, to: copied.key, reason: said(error) });
        continue;
      }
      done.moved.push({ from, to: copied.key });
      // Its original is in the bin: the copy in the chat's folder is the file now, no working copy to put back. One
      // still remembered is harmless: its put back finds the original gone.
      try {
        if (to.grant === null) this.options.bindings.forgetCopy(root, copied.key);
      } catch {
        // Kept: see above.
      }
    }
    for (const from of unticked(args.files, ticked)) done.skipped.push({ from, reason: UNTICKED });
    return { ok: done };
  }

  // *data* as a new file *name* in *folder*, the chat's folder or one in it, by its own tools, as a download is saved: a name that resolves
  // elsewhere, or holds anything, is passed over for the next, and the write itself only creates. Its key, or throws.
  private async intoChat(folder: string, name: string, data: string, inner: Inner): Promise<string> {
    let refused: string | null = null;
    for (let n = 0; n <= NAMES; n += 1) {
      const key = join(folder, numberedName(name, n));
      const resolved = await inner("resolve", { path: key });
      if ("error" in resolved) refused = resolved.error.message;
      if ("error" in resolved || resolved.ok !== key) continue;
      const found = await inner("stat", { key });
      if ("error" in found) throw new Failure(found.error);
      if (found.ok !== null) continue;
      const written = await inner("write", { key, data, create: true });
      if (!("error" in written)) return key;
      // Something made there since the look is left as it is, and the next name tried; otherwise the write's refusal.
      const now = await inner("stat", { key });
      if ("error" in now || now.ok === null) throw new Failure(written.error);
      refused = written.error.message;
    }
    throw new Failure({ type: "os", message: refused ?? `the chat's folder has ${NAMES} files of that name already` });
  }

  // *data* as a new file *name* in *to*, a folder in *grant*: reached a name at a time without following a link,
  // opened create-only (a file, folder or link at the name is a name taken), then read back and checked by size and
  // byte by byte. One that does not check out is removed: it is this copy's own, never a file that was there. Its key, or throws.
  private intoGrant(root: string, grant: string, to: string, name: string, data: string): string {
    const { fd } = this.options;
    const bytes = Buffer.from(data, "base64");
    for (let n = 0; n <= NAMES; n += 1) {
      const key = join(to, numberedName(name, n));
      // As the folder's own tools refuse it in the chat's folder (protect.ts, writeRefusal).
      if (fileOs.explorer(key)) throw new Failure({ type: "denied", message: explorerRefusal(key) });
      const made = this.within(root, grant, key, (where) => {
        const left: Made = { opened: false, made: null };
        try {
          this.createNew(where, bytes, "the copy did not check out against the original, so it was removed", left);
        } catch (error) {
          if (!left.opened) {
            if ((error as NodeJS.ErrnoException).code === "EEXIST") return false;
            throw fromNode(error, key);
          }
          const failed = fromNode(error, key);
          // Removed by its name once closed (Windows removes no open file), and only while that name is still this
          // copy's own file: one put there since is left alone.
          this.removeIfMine(where, left.made);
          throw failed;
        }
        return true;
      });
      if (made) return key;
    }
    throw new Failure({ type: "os", message: `${to} has ${NAMES} files of that name already` });
  }

  // Whether *path* is the chat's folder, holds it, or reaches it by another spelling: a move's never, though it may move a
  // file in it out.
  private holdsChat(root: string, path: string, look: Look): boolean {
    const folder = this.options.bindings.get(root)?.folder;
    return folder !== undefined && (inside(folder, path) || this.reachesChat(root, path, look));
  }

  // Whether *path* is the chat's folder, in it, or holds it: its own tools' to delete, never this layer's.
  private chatOwns(root: string, path: string, look: Look): boolean {
    const folder = this.options.bindings.get(root)?.folder;
    return folder !== undefined && (inside(path, folder) || inside(folder, path) || this.reachesChat(root, path, look));
  }

  // Whether *path* reaches the chat's folder by a spelling of its own: on a drive that ignores case, or knows a short
  // name (WORK~1), a grant above the chat's folder spells it many ways, and only its identity tells. Each name from the
  // grant down is looked at, none followed; reach() checks the folders it holds again, as it uses them.
  private reachesChat(root: string, path: string, look: Look): boolean {
    const binding = this.options.bindings.get(root);
    // Spelled as its own, it is the chat's by its path, as every caller tells first.
    const grant = binding === undefined || inside(path, binding.folder) ? null : look(path);
    if (grant === null) return false;
    const ids = this.chatIds(root);
    for (let at = path; ; at = at.slice(0, at.lastIndexOf("/"))) {
      try {
        if (isChat(ids, this.options.fd.lstat(at))) return true;
      } catch {
        // Not there: nothing it is.
      }
      if (at === grant) return false;
    }
  }

  // Why *copy* may not be put back over *original*, or null: the original a grant's file, not the chat's, and the copy
  // one this chat made of it.
  private putBackRefusal(root: string, copy: string, original: string, look: Look): Outcome | null {
    const grant = look(original);
    if (this.chatOwns(root, original, look)) return chatsOwn(original);
    if (grant === null) return noPermission(original);
    if (this.options.bindings.copyOf(root, copy)?.original !== original) {
      return denied(`${copy} is not a copy of ${original} made in this chat, so it cannot be put back there.`);
    }
    return null;
  }

  // The files its user ticked, each to the bin on its own: one that cannot go is kept, said why, and the rest go.
  private async trash(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    const root = operation.sessionId;
    // Let go before anything else: a trash refused here is not done later with them.
    const ticked = this.options.approvals.chosenFor(operation.id);
    // Taken up again after a restart, its user's ticks are gone: nothing goes that nobody chose.
    if (ticked === undefined) return denied(`Nothing was moved to the ${BIN}: the files its user chose are not known any more. Ask again.`);
    const done: { trashed: string[]; kept: Array<{ file: string; reason: string }> } = { trashed: [], kept: [] };
    for (const file of ticked) {
      try {
        // Each file against the grants as they are when its turn comes: one taken back while the bin had another is seen.
        await this.trashOne(root, file, this.looker(root), undefined, signal);
        done.trashed.push(file);
      } catch (error) {
        done.kept.push({ file, reason: said(error) });
      }
    }
    for (const file of unticked(operation.args.files, ticked)) done.kept.push({ file, reason: UNTICKED });
    return { ok: done };
  }

  // *file*, in a grant, to the bin; with *revision*, only while it is still the file as it was read then. Or throws.
  private async trashOne(root: string, file: string, look: Look, revision: string | undefined, signal: AbortSignal): Promise<void> {
    if (signal.aborted) throw osFailure("stopped");
    // Checked again, as a read is: the chat's own, a file now in no grant, and a link put at its name since stay.
    const grant = look(file);
    if (this.chatOwns(root, file, look)) throw failureOf(chatsOwn(file));
    if (grant === null) throw failureOf(noPermission(file));
    this.within(root, grant, file, (where) => {
      const st = io(file, () => this.options.fd.lstat(where));
      if (st.isSymbolicLink()) throw osFailure(`${file} is a link now, not the file its user chose`);
      // A folder, the grant's own among them, is no file its user was asked about.
      if (!st.isFile()) throw osFailure("not a file");
      if (revision !== undefined && revisionOf(st) !== revision) throw osFailure(`${file} changed after it was copied, so it was kept where it is`);
    });
    // ponytail: the bin takes a path, so a link swapped in after the look above and before the move is moved itself
    // (a link is moved, never followed); held by a handle once the OS's trash takes one.
    await this.options.trash(hostPathOf(file)).catch((error: unknown) => { throw binError(error); });
  }

  // A copy's changes put back over its original, in order: the original unchanged since it was copied, the changes
  // made a new file beside it and checked, the original to the bin, the new file given its name and checked again, and
  // only then the copy deleted. Before the bin nothing is changed; after it, the copy stays until its changes are in place.
  private async putBack(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    const { args, sessionId: root } = operation;
    const { copy, original } = args;
    if (typeof copy !== "string" || typeof original !== "string") return { error: { type: "value", message: "'copy' and 'original' must be strings" } };
    // Checked again: an operation reaches here without its refusal asked, as one taken up again after a restart.
    const look = this.looker(root);
    const refused = this.putBackRefusal(root, copy, original, look);
    if (refused !== null) return refused;
    const { revision } = this.options.bindings.copyOf(root, copy)!;
    const grant = look(original)!;
    const { fd } = this.options;
    const conflict: Outcome = { error: { type: "conflict", message: `${original} changed since it was copied. Nothing was replaced; the changes are in ${copy}.` } };
    const notReplaced = (cause: string): Outcome => ({ error: { type: "os", message: `${original} was not replaced: ${cause}. Nothing was changed; the changes are still in ${copy}.` } });
    const inBin: Outcome = { error: { type: "os", message: `The old version is in the ${BIN}; the changes are still in ${copy}.` } };
    const gone: Outcome = { error: { type: "conflict", message: `${original} is no longer there; the old version may already be in the ${BIN}. The changes are in ${copy}.` } };
    const inner = this.inner(operation, "outside-put-back", signal);
    // The original as it is now, if it is still the file copied; null for one changed since.
    const unchanged = (where: Where): BigIntStats | null => {
      const st = fd.lstat(where);
      return !st.isSymbolicLink() && revisionOf(st) === revision ? st : null;
    };
    let reached: { where: Where; release: () => void };
    try {
      reached = this.reach(root, grant, original);
    } catch (error) {
      // The chat's folder by another name is refused as such.
      if (error instanceof Failure && error.refusal.type === "denied") return { error: error.refusal };
      return notReplaced(said(error));
    }
    const { where, release } = reached;
    try {
      if (typeof where === "string") return notReplaced(`${original} is a folder`);
      const { dir } = where;
      let before: BigIntStats | null;
      try {
        before = unchanged(where);
      } catch (error) {
        // Gone: maybe a put back that stopped between the bin and the rename.
        return (error as NodeJS.ErrnoException).code === "ENOENT" ? gone : conflict;
      }
      if (before === null) return conflict;
      const read = await inner("read", { key: copy, max_bytes: null });
      if ("error" in read) return notReplaced(read.error.message);
      const bytes = Buffer.from(read.ok as string, "base64");
      const temporary = { dir, name: temporaryName() };
      const left: Made = { opened: false, made: null };
      // The temporary file's name goes on every way out, the one that put it in place too: only while it is still this
      // put back's own, or, where its identity could not be read, as just made by a create-only open.
      const removeTemporary = () => this.removeIfMine(temporary, left.made, left.opened);
      try {
        this.createNew(temporary, bytes, "the changes did not check out once written", left, (file) => {
          // The original's permissions, where the OS keeps them (Windows keeps one read-only bit, its own to set).
          if (process.platform !== "win32") {
            try {
              fchmodSync(file, Number(before.mode) & 0o777);
            } catch {
              // A drive that keeps no permissions (FAT, exFAT, some shares) refuses: the file keeps the drive's own.
            }
          }
        });
        // Looked at again just before the bin: an edit saved while the changes were written is not sent there.
        if (signal.aborted) throw osFailure("it was stopped");
        if (unchanged(where) === null) {
          removeTemporary();
          return conflict;
        }
      } catch (error) {
        removeTemporary();
        return notReplaced(said(fromNode(error, original)));
      }
      try {
        // ponytail: the bin takes a path, as outside.trash's does: a folder on the way swapped after the look above sends
        // what is there now instead, and the link below then finds the name taken and replaces nothing.
        await this.options.trash(hostPathOf(original)).catch((error: unknown) => { throw binError(error); });
      } catch (error) {
        removeTemporary();
        return { error: { type: "os", message: `${original} could not be moved to the ${BIN}: ${said(error)}. Nothing was replaced; the changes are still in ${copy}.` } };
      }
      try {
        this.renamed(temporary, where, left.made!);
        // Checked from the file now at the original's name, held open: the changes, whole, in this put back's own file.
        const final = fd.open(where, constants.O_RDONLY | FLAGS.NOFOLLOW);
        try {
          if (!sameFile(left.made!, fd.fstat(final, { bigint: true })) || !this.holds(final, bytes)) throw osFailure("the file put back did not check out");
        } finally {
          closeSync(final);
        }
      } catch {
        removeTemporary();
        return inBin;
      }
      // A second name of the file now in place, where the rename's own removal of it failed.
      removeTemporary();
      // In place and checked: the working copy goes, by the chat's folder's own tools, and its record with it.
      const deleted = await inner("delete", { key: copy });
      this.options.bindings.forgetCopy(root, copy);
      return { ok: { original, copyDeleted: !("error" in deleted) } };
    } finally {
      release();
    }
  }

  // A new file at *where*: opened create-only, no link followed (a name taken throws EEXIST), *prepare*d by its handle,
  // and *bytes* written and checked (writeChecked), then closed. *left* says whether it opened and what it made, for
  // its caller to remove it by on a failure (removeIfMine).
  private createNew(where: Where, bytes: Buffer, mismatch: string, left: Made, prepare?: (file: number) => void): void {
    const { fd } = this.options;
    const file = fd.open(where, constants.O_RDWR | constants.O_CREAT | constants.O_EXCL | FLAGS.NOFOLLOW, 0o666);
    left.opened = true;
    try {
      left.made = fd.fstat(file, { bigint: true });
      prepare?.(file);
      this.writeChecked(file, bytes, mismatch);
    } finally {
      closeSync(file);
    }
  }

  // The name *where* removed only while it is the file *made*; where *made* could not be read, only when *unseen*
  // says the name is one a create-only open just made.
  private removeIfMine(where: Where, made: BigIntStats | null, unseen = false): void {
    const { fd } = this.options;
    try {
      if (made !== null ? sameFile(made, fd.lstat(where)) : unseen) fd.unlink(where);
    } catch {
      // Gone already.
    }
  }

  // *bytes* written to the new, empty *file*, then read back from it and checked by size and byte by byte: by its handle, so
  // what is checked is what was written, wherever its name leads now. Throws *mismatch* if it does not check out.
  private writeChecked(file: number, bytes: Buffer, mismatch: string): void {
    for (let written = 0; written < bytes.length;) written += writeSync(file, bytes, written, bytes.length - written, written);
    if (!this.holds(file, bytes)) throw osFailure(mismatch);
  }

  // Whether the file open as *file* holds exactly *bytes*.
  private holds(file: number, bytes: Buffer): boolean {
    const back = Buffer.alloc(bytes.length + 1);
    let got = 0;
    for (let count = -1; count !== 0 && got < back.length; got += count) count = readSync(file, back, got, back.length - got, got);
    return this.options.fd.fstat(file).size === bytes.length && got === bytes.length && back.subarray(0, got).equals(bytes);
  }

  // *from* given the name *to* in the same folder, and never over a file there: a hard link fails where the name is
  // taken, on every OS (link(2); FileLinkInformation without ReplaceIfExists), and *from*'s own name then goes.
  private renamed(from: Where, to: Where, made: BigIntStats): void {
    const { fd } = this.options;
    try {
      fd.link(from, to);
    } catch (error) {
      const { code } = error as NodeJS.ErrnoException;
      if (code === "EEXIST" || !["ENOTSUP", "EOPNOTSUPP", "EPERM", "ENOSYS"].includes(code ?? "")) throw error;
      // No hard links on this drive (FAT, exFAT): looked at, then renamed.
      // ponytail: a file made at *to* between the look and the rename is replaced; a no-replace rename shuts that once fd-paths has one.
      let taken = true;
      try {
        fd.lstat(to);
      } catch {
        taken = false;
      }
      if (taken || !sameFile(made, fd.lstat(from))) throw error;
      fd.rename(from, to);
      return;
    }
    try {
      fd.unlink(from);
    } catch {
      // Left as a second name of the file put back: the caller removes it once the file checks out.
    }
  }

  private read(kind: string, args: Record<string, unknown>, grant: string, key: string): unknown | Promise<unknown> {
    const { fd } = this.options;
    const opener = (where: Where) => (flags: number) => fd.open(where, flags);
    switch (kind) {
      case "outside.resolve":
        return key;
      case "outside.stat":
        try {
          // A link put there since is no file of the grant's: answered as nothing there, as a stat that fails is.
          return this.within(null, grant, key, (where) => {
            const st = fd.lstat(where);
            return st.isSymbolicLink() ? null : statOf(st);
          });
        } catch {
          return null;
        }
      case "outside.read":
        // Opened by the same walk down, its folder held only for the open: readFromAsync opens before its first await,
        // then reads its pieces off the main thread from the handle.
        return this.within(null, grant, key, (where) => readFromAsync(args, key, opener(where)));
      case "outside.read_lines":
        return this.within(null, grant, key, (where) => linesFrom(args, key, opener(where)));
      case "outside.list_dir":
        return this.within(null, grant, key, (where) => {
          const held = io(key, () => fd.hold(where));
          try {
            return listed(io(key, () => fd.readdir({ dir: held })));
          } finally {
            closeSync(held);
          }
        });
      default: // outside.walk
        return this.within(null, grant, key, (where) =>
          walkFrom(args, key, opener(where), fd, {
            budgetMs: OUTSIDE_WALK_MS, maxFiles: OUTSIDE_WALK_FILES, times: true, folders: true,
          }));
    }
  }

  // *use* given *key* as reached from *grant*: the grant's own folder by its path, which withinGrant checked; anything
  // in it by its name in its folder, held open, each folder from the grant down held in turn and never through a link.
  private within<T>(root: string | null, grant: string, key: string, use: (where: Where) => T): T {
    const { where, release } = this.reach(root, grant, key);
    try {
      return use(where);
    } finally {
      release();
    }
  }

  // The chat's folder's identities, device and inode both: the binder's, read through the walk (on Windows its device is
  // the volume's serial, which a reboot keeps), and, on Linux and macOS, the folder's own now, by its path with no link
  // followed at its name, since a reboot can renumber a mount's device there. None for no root, or an unbound one.
  private chatIds(root: string | null): Identity<bigint>[] {
    const binding = root === null ? undefined : this.options.bindings.get(root);
    if (binding === undefined) return [];
    const ids = [{ dev: BigInt(binding.dev), ino: BigInt(binding.ino) }];
    if (process.platform !== "win32") {
      try {
        ids.push(identityOf(this.options.fd.lstat(binding.folder)));
      } catch {
        // Gone: the binder's identity alone.
      }
    }
    return ids;
  }

  // *key* as within reaches it, its folder held until *release*: for a use that waits on something in between. For a
  // *root* that changes files (a trash, a put back, a copy), each folder held on the way, and *key* itself, is checked
  // not to be its chat's folder by identity: by another spelling of its path (case, a short name) that is the chat's
  // own, its tools' to change, never this layer's. A read (root null) may reach the chat's own files any way: it harms nothing.
  private reach(root: string | null, grant: string, key: string): { where: Where; release: () => void } {
    const { fd } = this.options;
    const ids = this.chatIds(root);
    const notChat = (stats: () => BigIntStats) => {
      let st: BigIntStats;
      try {
        st = stats();
      } catch {
        return; // nothing there yet, as a copy's new name: no folder of the chat's
      }
      if (isChat(ids, st)) throw new Failure({ type: "denied", message: byAnotherName(key) });
    };
    if (key === grant) {
      notChat(() => fd.lstat(grant));
      return { where: grant, release: () => {} };
    }
    const names = key.slice(grant.length + 1).split("/");
    let dir = io(key, () => fd.hold(grant));
    try {
      notChat(() => fd.fstat(dir, { bigint: true }));
      for (const name of names.slice(0, -1)) {
        const next = io(key, () => fd.hold({ dir, name }));
        closeSync(dir);
        dir = next;
        notChat(() => fd.fstat(dir, { bigint: true }));
      }
      const last = { dir, name: names.at(-1)! };
      notChat(() => fd.lstat(last));
    } catch (error) {
      closeSync(dir);
      throw error;
    }
    const held = dir;
    return { where: { dir: held, name: names.at(-1)! }, release: () => closeSync(held) };
  }

  // An outcome as the file helper's perform() makes one: a failure in its words, and a result too large to send refused.
  private async answer(kind: string, call: () => unknown): Promise<Outcome> {
    let outcome: Outcome;
    try {
      outcome = { ok: (await call()) ?? null };
    } catch (error) {
      outcome = {
        error: error instanceof Failure
          ? error.refusal
          : { type: "other", message: error instanceof Error ? `${error.name}: ${error.message}` : String(error) },
      };
    }
    // A read's data over the payload goes as a transfer (runner.ts, RESULT_TRANSFERS), as the helper's does.
    if (kind !== "outside.read" && JSON.stringify(outcome).length > MAX_MESSAGE_CHARS) {
      return { error: { type: "too_large", message: `The result of ${kind} is too large` } };
    }
    return outcome;
  }

  guards(): FolderGuards {
    return this.options.tools.guards();
  }

  live(): string[] {
    return this.options.tools.live();
  }

  stop(): Promise<void> {
    return this.options.tools.stop();
  }
}

// Whether *stats* are of the chat's folder, one of *ids*: by device and inode both, as an inode alone repeats across drives.
function isChat(ids: Identity<bigint>[], stats: BigIntStats): boolean {
  const { dev, ino } = identityOf(stats);
  return ids.some((id) => id.dev === dev && id.ino === ino);
}

// The path a read kind names: resolve's as the agent wrote it, made plain, which must be absolute; every other's key,
// which must already be plain (withinGrant refuses any other).
function pathOf({ kind, args }: Operation): string | Outcome {
  const name = kind === "outside.resolve" ? "path" : "key";
  const value = args[name];
  if (typeof value !== "string") return { error: { type: "value", message: `'${name}' must be a string` } };
  if (kind !== "outside.resolve") return value;
  if (!isAbsolute(value)) return { error: { type: "sandbox", message: `'${value}' is outside the chat's folder only as an absolute path` } };
  return resolve(value);
}

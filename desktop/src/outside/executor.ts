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

import { createHash, randomUUID } from "node:crypto";
import { type BigIntStats, closeSync, constants, readSync, writeSync } from "node:fs";
import { extname } from "node:path";

import type { Approvals } from "../binding/approvals.js";
import type { FolderGuards } from "../binding/folder.js";
import { Failure, FOLDER_UNAVAILABLE, fromNode, io, MAX_MESSAGE_CHARS, READ_TOO_LARGE } from "../files/answers.js";
import type { FdPaths, Where } from "../files/fd-paths.js";
import { FLAGS } from "../files/flags.js";
import { linesFrom, listed, readFrom, revisionOf, statOf, walkFrom } from "../files/operations.js";
import { sameFile } from "../files/identity.js";
import { inside } from "../files/paths.js";
import type { Bindings } from "../journal/bindings.js";
import type { Operation, Outcome } from "../link/protocol.js";
import { basename, hostPathOf, isAbsolute, join, resolve } from "../session-path.js";
import type { ToolLayer } from "../shell/device-stack.js";
import { grantable, KNOWN_FOLDERS, knownFolder, withinGrant } from "./places.js";

const OUTSIDE = "outside.";
const READS: ReadonlySet<string> = new Set([
  "outside.resolve", "outside.stat", "outside.read", "outside.read_lines", "outside.list_dir", "outside.walk",
]);

const noPermission = (path: string): Outcome => ({
  error: { type: "sandbox", message: `No permission to read ${path}: ask for it first with request_folder_access.` },
});
const denied = (message: string): Outcome => ({ error: { type: "denied", message } });
const unsupported = (kind: string): Outcome => ({ error: { type: "unsupported", message: `This computer cannot do '${kind}' yet` } });
const chatsOwn = (path: string): Outcome => denied(`${path} is this chat's folder, in it, or holds it: the chat's own files are deleted with its own tools.`);
const notDestination = (to: string): Outcome => denied(`The destination ${to} is neither this chat's folder nor a folder it may read.`);
// How many numbered names a copy tries past its own, as a download does.
const NAMES = 100;
const TOO_LARGE = "too large";
const sha256 = (buffer: Buffer) => createHash("sha256").update(buffer).digest("hex");
const said = (error: unknown) => error instanceof Failure ? error.refusal.message : error instanceof Error ? error.message : String(error);
const osFailure = (message: string) => new Failure({ type: "os", message });
// Why a trash or a rename failed, in words its user can act on: Windows refuses either for a file open in another program.
const causeOf = (error: unknown) => ["EBUSY", "EPERM"].includes((error as NodeJS.ErrnoException).code ?? "") ? "it is open in another program" : said(error);

/** The *n*th name a copy of *name* tries: its own first, then "name (1).ext", "name (2).ext", and on. */
export function numberedName(name: string, n: number): string {
  if (n === 0) return name;
  const extension = extname(name);
  return `${name.slice(0, name.length - extension.length)} (${n})${extension}`;
}

// What a copy read: the file's bytes, base64, and, for one from a grant, its revision as read.
type Source = { data: string; revision?: string };
type Copied = { copied: Array<{ from: string; to: string }>; skipped: Array<{ from: string; reason: string }> };

export interface OutsideOptions {
  tools: ToolLayer;
  bindings: Bindings;
  approvals: Pick<Approvals, "chosenFor">;
  fd: FdPaths;
  getPath: (name: "desktop" | "downloads" | "documents" | "pictures") => string;
  /** The file at this computer's own *hostPath* moved to its Recycle Bin (Electron's shell.trashItem); rejects if not. */
  trash: (hostPath: string) => Promise<void>;
}

// Every other member of the tools below, there only where they have it: a caller tells one they lack by its absence.
const PASSED = [
  "retired", "keepsCopies", "folderInUse", "address", "notComing", "takeOver", "handBack", "takenOver", "heldFromGone", "handBackGone",
  "show", "saveDownloadsWith", "forwarded", "listening", "admit", "acknowledged", "end",
] as const satisfies ReadonlyArray<keyof ToolLayer>;
type Passed = (typeof PASSED)[number];
// A member ToolLayer gains is passed down, or this fails to compile.
const EVERY: [Exclude<keyof ToolLayer, Passed | "run" | "refusal" | "guards" | "live" | "stop">] extends [never] ? true : never = true;
void EVERY;

// The members the constructor assigns from PASSED.
export interface Outside extends Pick<ToolLayer, Passed> {}

export class Outside implements ToolLayer {
  constructor(private readonly options: OutsideOptions) {
    const { tools } = options;
    for (const name of PASSED) {
      const member = tools[name] as ((...args: never[]) => unknown) | undefined;
      if (member) Object.assign(this, { [name]: member.bind(tools) });
    }
  }

  /**
   * The folder an outside.grant names, as its window shows it and its grant keeps it: a known folder ("Desktop") where
   * the OS keeps it, or an absolute path, as it resolves; or why the chat *root* may not be granted it.
   */
  resolveFolder(folder: string, root: string): { folder: string } | { refused: string } {
    const binding = this.options.bindings.get(root);
    if (!binding) return { refused: (FOLDER_UNAVAILABLE as { error: { message: string } }).error.message };
    const known = KNOWN_FOLDERS.some((name) => name.toLowerCase() === folder.toLowerCase());
    const path = known ? knownFolder(folder, this.options.getPath) : isAbsolute(folder) ? folder : null;
    if (path === null) {
      return {
        refused: known
          ? `Surogate cannot read ${folder}: this computer keeps it on no drive of its own.`
          : `'${folder}' is not a folder Surogate can ask for: name Desktop, Downloads, Documents or Pictures, or give an absolute path.`,
      };
    }
    const granted = grantable(path, this.options.tools.guards(), binding.folder);
    if (granted.ok) return { folder: granted.folder };
    // checkFolder's words are a chat folder's, and start small: said of a grant to read instead.
    const { message } = granted;
    return { refused: /^[a-z]/.test(message) ? `Surogate cannot read ${path}: ${message.replace(/cannot be a chat's$/, "cannot be read")}` : message };
  }

  refusal(operation: Operation): Outcome | null {
    const { kind, args, sessionId: root } = operation;
    if (!kind.startsWith(OUTSIDE)) return this.options.tools.refusal?.(operation) ?? null;
    // An unbound chat's, and a project thread's, the approvals answer in their own words.
    const binding = this.options.bindings.get(root);
    if (!binding || binding.history !== undefined) return null;
    if (kind === "outside.grant") {
      if (typeof args.folder !== "string") return denied("'folder' must be a folder's name or an absolute path.");
      const resolved = this.resolveFolder(args.folder, root);
      return "refused" in resolved ? denied(resolved.refused) : null;
    }
    if (READS.has(kind)) {
      const path = pathOf(operation);
      if (typeof path !== "string") return path;
      return this.grantOf(root, path) === null ? noPermission(path) : null;
    }
    if (kind === "outside.trash" && Array.isArray(args.files)) {
      const own = args.files.find((file) => typeof file === "string" && this.chatOwns(root, file)) as string | undefined;
      if (own !== undefined) return chatsOwn(own);
    }
    if (kind === "outside.put_back") return typeof args.copy === "string" && typeof args.original === "string" ? this.putBackRefusal(root, args.copy, args.original) : null;
    if ((kind === "outside.copy" || kind === "outside.trash") && Array.isArray(args.files)) {
      // Malformed ones are the approvals' to refuse, in their words. A copy may take from the chat's folder too.
      const out = args.files.find((file) => typeof file === "string" && this.sourceOf(root, file, kind === "outside.copy") === null) as string | undefined;
      if (out !== undefined) return noPermission(out);
    }
    if (kind === "outside.copy" && typeof args.to === "string" && this.destinationOf(root, args.to) === null) return notDestination(args.to);
    return null;
  }

  async run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    const { kind, args, sessionId: root } = operation;
    if (!kind.startsWith(OUTSIDE)) return this.options.tools.run(operation, signal);
    if (!this.options.bindings.get(root)) return FOLDER_UNAVAILABLE;
    if (kind === "outside.grant") {
      // Kept by the approvals on its user's allow: answered with where, once it is kept.
      if (typeof args.folder !== "string") return denied("'folder' must be a folder's name or an absolute path.");
      const resolved = this.resolveFolder(args.folder, root);
      if ("refused" in resolved) return denied(resolved.refused);
      return this.options.bindings.reads(root).includes(resolved.folder) ? { ok: { folder: resolved.folder } } : noPermission(resolved.folder);
    }
    if (kind === "outside.copy") return this.copy(operation, signal);
    if (kind === "outside.trash") return this.trash(operation, signal);
    if (kind === "outside.put_back") return this.putBack(operation, signal);
    if (!READS.has(kind)) return unsupported(kind);
    // Checked again: an operation reaches here without its refusal asked, as one taken up again after a restart.
    const key = pathOf(operation);
    if (typeof key !== "string") return key;
    const grant = this.grantOf(root, key);
    if (grant === null) return noPermission(key);
    return this.answer(kind, () => this.read(kind, args, grant, key));
  }

  // The grant of *root*'s that holds *path*, no link on its way; null for none. On Windows a folder is looked at only
  // once the file host bound it: each grant is bound here, which keeps its folder held for the app's life, and does
  // nothing for one bound already. A grant taken back is still bound, and is in no list of grants, so it holds nothing.
  private grantOf(root: string, path: string): string | null {
    const grants = this.options.bindings.reads(root);
    for (const grant of grants) {
      try {
        this.options.fd.bind([grant]);
      } catch {
        // Gone, or no longer where its path leads: withinGrant finds nothing in it.
      }
    }
    return withinGrant(grants, path, this.options.fd);
  }

  // Where a copy's *file* is read from: the chat's folder ("chat"), for a file in it, or the grant that holds it; null
  // for neither. The chat's folder is no grant's: a file there is the chat's own, read by its own tools.
  private sourceOf(root: string, file: string, chat = true): "chat" | string | null {
    const folder = this.options.bindings.get(root)?.folder;
    if (chat && folder !== undefined && file !== folder && inside(file, folder)) return "chat";
    return this.grantOf(root, file);
  }

  // Where a copy may go, as the folder it goes in: the chat's folder or one in it (grant null), written by its own
  // tools even where a grant holds the chat's folder too; or a folder in a grant, by that grant. Null for anywhere else.
  private destinationOf(root: string, to: string): { dir: string; grant: string | null } | null {
    const folder = this.options.bindings.get(root)?.folder;
    if (folder !== undefined && (to === "." || inside(to, folder))) return { dir: to === "." ? folder : to, grant: null };
    const grant = this.grantOf(root, to);
    return grant === null ? null : { dir: to, grant };
  }

  // The files its user ticked, each copied on its own: one that cannot be is said, and the rest are copied all the same.
  private async copy(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    const { args, sessionId: root } = operation;
    // Let go before anything else: a copy refused here is not done later with them.
    const ticked = this.options.approvals.chosenFor(operation.id);
    if (typeof args.to !== "string") return { error: { type: "value", message: "'to' must be a string" } };
    // Checked again: an operation reaches here without its refusal asked, as one taken up again after a restart.
    const to = this.destinationOf(root, args.to);
    if (to === null) return notDestination(args.to);
    // Taken up again after a restart, its user's ticks are gone: nothing is copied that nobody chose.
    if (ticked === undefined) return denied("Nothing was copied: the files its user chose are not known any more. Ask again.");
    const done: Copied = { copied: [], skipped: [] };
    const inner = (kind: string, opArgs: Record<string, unknown>): Promise<Outcome> => this.options.tools.run({
      ...operation, id: `outside-copy-${randomUUID()}`, kind, args: opArgs, digest: "",
    }, signal);
    for (const from of ticked) {
      if (signal.aborted) {
        done.skipped.push({ from, reason: "stopped" });
        continue;
      }
      try {
        const source = this.sourceOf(root, from);
        if (source === null) throw new Failure((noPermission(from) as { error: { type: string; message: string } }).error);
        let read: Source;
        if (source === "chat") {
          const outcome = await inner("read", { key: from, max_bytes: null });
          if ("error" in outcome) throw new Failure(outcome.error);
          read = { data: outcome.ok as string };
        } else {
          read = this.within(source, from, (where) => ({
            // Looked at before the read: a change in between makes the revision older than the bytes, and a put back
            // then finds the original changed, and refuses; never the other way round.
            revision: revisionOf(io(from, () => this.options.fd.lstat(where))),
            data: readFrom({ max_bytes: null }, from, (flags) => this.options.fd.open(where, flags)),
          }));
        }
        const name = basename(from);
        const key = to.grant === null ? await this.intoChat(to.dir, name, read.data, inner) : this.intoGrant(to.grant, to.dir, name, read.data);
        // A copy of an outside file is remembered for its put back; any other made at a key once a copy's is no copy.
        if (to.grant === null) {
          if (source === "chat") this.options.bindings.forgetCopy(root, key);
          else this.options.bindings.recordCopy(root, key, from, read.revision!);
        }
        done.copied.push({ from, to: key });
      } catch (error) {
        const said = error instanceof Failure ? error.refusal.message : error instanceof Error ? error.message : String(error);
        done.skipped.push({ from, reason: said === READ_TOO_LARGE ? TOO_LARGE : said });
      }
    }
    return { ok: done };
  }

  // *data* as a new file *name* in *folder*, the chat's folder or one in it, by its own tools, as a download is saved: a name that resolves
  // elsewhere, or holds anything, is passed over for the next, and the write itself only creates. Its key, or throws.
  private async intoChat(folder: string, name: string, data: string, inner: (kind: string, args: Record<string, unknown>) => Promise<Outcome>): Promise<string> {
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
  // sha256. One that does not check out is removed: it is this copy's own, never a file that was there. Its key, or throws.
  private intoGrant(grant: string, to: string, name: string, data: string): string {
    const { fd } = this.options;
    const bytes = Buffer.from(data, "base64");
    for (let n = 0; n <= NAMES; n += 1) {
      const key = join(to, numberedName(name, n));
      const made = this.within(grant, key, (where) => {
        let file: number;
        try {
          file = fd.open(where, constants.O_RDWR | constants.O_CREAT | constants.O_EXCL | FLAGS.NOFOLLOW, 0o666);
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code === "EEXIST") return false;
          throw fromNode(error, key);
        }
        let failed: unknown = null;
        let made: BigIntStats | null = null;
        try {
          made = fd.fstat(file, { bigint: true });
          this.writeChecked(file, bytes, "the copy did not check out against the original, so it was removed");
        } catch (error) {
          failed = fromNode(error, key);
        } finally {
          closeSync(file);
        }
        if (failed !== null) {
          // Removed by its name once closed (Windows removes no open file), and only while that name is still this
          // copy's own file: one put there since is left alone.
          try {
            if (made !== null && sameFile(made, fd.lstat(where))) fd.unlink(where);
          } catch {
            // Gone already.
          }
          throw failed;
        }
        return true;
      });
      if (made) return key;
    }
    throw new Failure({ type: "os", message: `${to} has ${NAMES} files of that name already` });
  }

  // Whether *path* is the chat's folder, in it, or holds it: its own tools' to delete, never this layer's.
  private chatOwns(root: string, path: string): boolean {
    const folder = this.options.bindings.get(root)?.folder;
    return folder !== undefined && (inside(path, folder) || inside(folder, path));
  }

  // Why *copy* may not be put back over *original*, or null: the original a grant's file, not the chat's, and the copy
  // one this chat made of it.
  private putBackRefusal(root: string, copy: string, original: string): Outcome | null {
    if (this.chatOwns(root, original)) return chatsOwn(original);
    if (this.grantOf(root, original) === null) return noPermission(original);
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
    if (ticked === undefined) return denied("Nothing was moved to the Recycle Bin: the files its user chose are not known any more. Ask again.");
    const done: { trashed: string[]; kept: Array<{ file: string; reason: string }> } = { trashed: [], kept: [] };
    for (const file of ticked) {
      try {
        if (signal.aborted) throw osFailure("stopped");
        // Checked again, as a read is: the chat's own, a file now in no grant, and a link put at its name since stay.
        if (this.chatOwns(root, file)) throw new Failure((chatsOwn(file) as { error: { type: string; message: string } }).error);
        const grant = this.grantOf(root, file);
        if (grant === null) throw new Failure((noPermission(file) as { error: { type: string; message: string } }).error);
        this.within(grant, file, (where) => {
          if (io(file, () => this.options.fd.lstat(where)).isSymbolicLink()) throw osFailure(`${file} is a link now, not the file its user chose`);
        });
        // ponytail: the bin takes a path, so a link swapped in after the look above and before the move is moved itself
        // (a link is moved, never followed); held by a handle once the OS's trash takes one.
        await this.options.trash(hostPathOf(file));
        done.trashed.push(file);
      } catch (error) {
        done.kept.push({ file, reason: causeOf(error) });
      }
    }
    return { ok: done };
  }

  // A copy's changes put back over its original, in order: the original unchanged since it was copied, the changes
  // made a new file beside it and checked, the original to the bin, the new file given its name and checked again, and
  // only then the copy deleted. Before the bin nothing is changed; after it, the copy stays until its changes are in place.
  private async putBack(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    const { args, sessionId: root } = operation;
    const { copy, original } = args;
    if (typeof copy !== "string" || typeof original !== "string") return { error: { type: "value", message: "'copy' and 'original' must be strings" } };
    // Checked again: an operation reaches here without its refusal asked, as one taken up again after a restart.
    const refused = this.putBackRefusal(root, copy, original);
    if (refused !== null) return refused;
    const { revision } = this.options.bindings.copyOf(root, copy)!;
    const grant = this.grantOf(root, original)!;
    const { fd } = this.options;
    const conflict: Outcome = { error: { type: "conflict", message: `${original} changed since it was copied. Nothing was replaced; the changes are in ${copy}.` } };
    const notReplaced = (cause: string): Outcome => ({ error: { type: "os", message: `${original} was not replaced: ${cause}. Nothing was changed; the changes are still in ${copy}.` } });
    const inBin: Outcome = { error: { type: "os", message: `The old version is in the Recycle Bin; the changes are still in ${copy}.` } };
    const inner = (kind: string, opArgs: Record<string, unknown>): Promise<Outcome> => this.options.tools.run({
      ...operation, id: `outside-put-back-${randomUUID()}`, kind, args: opArgs, digest: "",
    }, signal);
    const unchanged = (where: Where) => {
      const st = io(original, () => fd.lstat(where));
      return !st.isSymbolicLink() && revisionOf(st) === revision;
    };
    let reached: { where: Where; release: () => void };
    try {
      reached = this.reach(grant, original);
    } catch (error) {
      return notReplaced(said(error));
    }
    const { where, release } = reached;
    try {
      if (typeof where === "string") return notReplaced(`${original} is a folder`);
      const { dir, name } = where;
      try {
        if (!unchanged(where)) return conflict;
      } catch {
        return conflict; // gone, or no longer the file it was
      }
      const read = await inner("read", { key: copy, max_bytes: null });
      if ("error" in read) return notReplaced(read.error.message);
      const bytes = Buffer.from(read.ok as string, "base64");
      const temporary = { dir, name: `.${name}.surogate-put-back-${randomUUID().slice(0, 8)}` };
      let made: BigIntStats | null = null;
      // The temporary file goes on every way out but the one that gives it the original's name; only while it is still this put back's own.
      const removeTemporary = () => {
        try {
          if (made !== null && sameFile(made, fd.lstat(temporary))) fd.unlink(temporary);
        } catch {
          // Gone already.
        }
      };
      try {
        const file = fd.open(temporary, constants.O_RDWR | constants.O_CREAT | constants.O_EXCL | FLAGS.NOFOLLOW, 0o666);
        try {
          made = fd.fstat(file, { bigint: true });
          this.writeChecked(file, bytes, "the changes did not check out once written");
        } finally {
          closeSync(file);
        }
        // Looked at again just before the bin: an edit saved while the changes were written is not sent there.
        if (signal.aborted) throw osFailure("it was stopped");
        if (!unchanged(where)) {
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
        await this.options.trash(hostPathOf(original));
      } catch (error) {
        removeTemporary();
        return { error: { type: "os", message: `${original} could not be moved to the Recycle Bin: ${causeOf(error)}. Nothing was replaced; the changes are still in ${copy}.` } };
      }
      try {
        this.renamed(temporary, where, made!);
        // Checked from the file now at the original's name, held open: the changes, whole, in this put back's own file.
        const final = fd.open(where, constants.O_RDONLY | FLAGS.NOFOLLOW);
        try {
          if (!sameFile(made!, fd.fstat(final, { bigint: true })) || !this.holds(final, bytes)) throw osFailure("the file put back did not check out");
        } finally {
          closeSync(final);
        }
      } catch {
        removeTemporary();
        return inBin;
      }
      // In place and checked: the working copy goes, by the chat's folder's own tools, and its record with it.
      const deleted = await inner("delete", { key: copy });
      this.options.bindings.forgetCopy(root, copy);
      return { ok: { original, copyDeleted: !("error" in deleted) } };
    } finally {
      release();
    }
  }

  // *bytes* written to the new, empty *file*, then read back from it and checked by size and sha256: by its handle, so
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
    return this.options.fd.fstat(file).size === bytes.length && got === bytes.length && sha256(back.subarray(0, got)) === sha256(bytes);
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
      // A second name of the file put back, harmless; the caller's cleanup tries it again.
    }
  }

  private read(kind: string, args: Record<string, unknown>, grant: string, key: string): unknown {
    const { fd } = this.options;
    const opener = (where: Where) => (flags: number) => fd.open(where, flags);
    switch (kind) {
      case "outside.resolve":
        return key;
      case "outside.stat":
        try {
          // A link put there since is no file of the grant's: answered as nothing there, as a stat that fails is.
          return this.within(grant, key, (where) => {
            const st = fd.lstat(where);
            return st.isSymbolicLink() ? null : statOf(st);
          });
        } catch {
          return null;
        }
      case "outside.read":
        return this.within(grant, key, (where) => readFrom(args, key, opener(where)));
      case "outside.read_lines":
        return this.within(grant, key, (where) => linesFrom(args, key, opener(where)));
      case "outside.list_dir":
        return this.within(grant, key, (where) => {
          const held = io(key, () => fd.hold(where));
          try {
            return listed(io(key, () => fd.readdir({ dir: held })));
          } finally {
            closeSync(held);
          }
        });
      default: // outside.walk
        return this.within(grant, key, (where) => walkFrom(args, key, opener(where), fd));
    }
  }

  // *use* given *key* as reached from *grant*: the grant's own folder by its path, which withinGrant checked; anything
  // in it by its name in its folder, held open, each folder from the grant down held in turn and never through a link.
  private within<T>(grant: string, key: string, use: (where: Where) => T): T {
    const { where, release } = this.reach(grant, key);
    try {
      return use(where);
    } finally {
      release();
    }
  }

  // *key* as within reaches it, its folder held until *release*: for a use that waits on something in between.
  private reach(grant: string, key: string): { where: Where; release: () => void } {
    const { fd } = this.options;
    if (key === grant) return { where: grant, release: () => {} };
    const names = key.slice(grant.length + 1).split("/");
    let dir = io(key, () => fd.hold(grant));
    try {
      for (const name of names.slice(0, -1)) {
        const next = io(key, () => fd.hold({ dir, name }));
        closeSync(dir);
        dir = next;
      }
    } catch (error) {
      closeSync(dir);
      throw error;
    }
    const held = dir;
    return { where: { dir: held, name: names.at(-1)! }, release: () => closeSync(held) };
  }

  // An outcome as the file helper's perform() makes one: a failure in its words, and a result too large to send refused.
  private answer(kind: string, call: () => unknown): Outcome {
    let outcome: Outcome;
    try {
      outcome = { ok: call() ?? null };
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

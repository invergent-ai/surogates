// Folders outside a chat's folder (spec, Part A), done by the app itself: no sandbox binds them, so this layer is the
// containment. It answers outside.* under the binder, in the file helper's words (operations.ts), and hands every other
// kind to the tools below. A read is let through only in a folder its chat's user granted (approvals.ts asks for the
// grant), and only where no name on the way is a link (places.ts, withinGrant). That check holds nothing afterwards,
// so the read itself goes from the grant's folder one name at a time, each held without following a link: a link put
// on the way after the check leads the read nowhere. A file with two hard links is one file in both places, and no
// look tells which name is its own: one in a grant is read like any other.

import { closeSync } from "node:fs";

import type { Approvals } from "../binding/approvals.js";
import type { FolderGuards } from "../binding/folder.js";
import { Failure, FOLDER_UNAVAILABLE, io, MAX_MESSAGE_CHARS } from "../files/answers.js";
import type { FdPaths, Where } from "../files/fd-paths.js";
import { linesFrom, listed, readFrom, statOf, walkFrom } from "../files/operations.js";
import type { Bindings } from "../journal/bindings.js";
import type { Operation, Outcome } from "../link/protocol.js";
import { isAbsolute, resolve } from "../session-path.js";
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

export interface OutsideOptions {
  tools: ToolLayer;
  bindings: Bindings;
  approvals: Pick<Approvals, "chosenFor">;
  fd: FdPaths;
  getPath: (name: "desktop" | "downloads" | "documents" | "pictures") => string;
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
    if ((kind === "outside.copy" || kind === "outside.trash") && Array.isArray(args.files)) {
      // Malformed ones are the approvals' to refuse, in their words.
      const out = args.files.find((file) => typeof file === "string" && this.grantOf(root, file) === null) as string | undefined;
      return out === undefined ? null : noPermission(out);
    }
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
    if (kind === "outside.copy" || kind === "outside.trash") {
      // Not done here yet (copy and trash come later): the ticked files are let go all the same.
      this.options.approvals.chosenFor(operation.id);
      return unsupported(kind);
    }
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
    const { fd } = this.options;
    if (key === grant) return use(grant);
    const names = key.slice(grant.length + 1).split("/");
    let dir = io(key, () => fd.hold(grant));
    try {
      for (const name of names.slice(0, -1)) {
        const next = io(key, () => fd.hold({ dir, name }));
        closeSync(dir);
        dir = next;
      }
      return use({ dir, name: names.at(-1)! });
    } finally {
      closeSync(dir);
    }
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

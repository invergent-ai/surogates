// Git hooks a command left in the folder. Git runs an executable file in a .git
// folder's hooks folder outside the sandbox, the next time someone runs git there,
// and the guest's rule refuses the writes that would plant one. As a backstop,
// after every command the host looks through the whole folder and
// makes each hook that is not the user's own, unchanged, non-executable: git skips
// those. Nothing is deleted. The same look refuses commands while a protected name,
// or a key place, is a link to a path in the folder that is not protected (linkedInto),
// and while a link outside a dependency folder leads into one (leadsInto).
// It also comments out each exec step that appeared in a paused rebase's or
// cherry-pick's todo while the chat's commands could write there, which the host's
// git rebase --continue would run outside the sandbox: the guest's rule lets commands
// write git's transient state (stripTodo).

import { randomUUID } from "node:crypto";
import type { Stats } from "node:fs";
import { access, constants, lstat, open, readdir, readlink, realpath, rename, stat, unlink } from "node:fs/promises";
import { basename, dirname, join, relative } from "node:path";

import { inside, realpath as followed } from "../files/paths.js";
import { dependencyFolder, KEY_FOLDERS, protectedInFolder } from "../files/protect.js";
import type { Outcome } from "../link/protocol.js";

export const SCAN_TIMEOUT_MS = 30_000;
export const HOOKS_NOTICE = "The computer stopped these git hooks from running, because git would run them outside the sandbox (where it closed a hooks folder, that repository's other hooks are off until you make it searchable again): ";
const STEPS_NOTICE = "The computer removed a step that appeared in a paused git rebase or cherry-pick while this chat's commands could write there, because git would run it outside the sandbox; each such exec line is now a comment in: ";
const REMOVED = "# Surogate removed a step that appeared while the chat's commands could write: ";
export const MAX_LISTED = 20;
// An exec step, as git reads a todo's line: "exec" or "x", then a blank or the line's end.
const EXEC = /^[ \t]*(?:exec|x)(?:[ \t\r]|$)/;
// Far larger than any rebase's todo: one a command made larger is not read.
const MAX_TODO = 16 * 1024 * 1024;

export interface HookScan {
  // Each hook's key, and what tells whether it changed (see describe).
  hooks: Map<string, string>;
  // Folders that could not be read where a command could have hidden a hook.
  unreadable: string[];
  // Each link at a protected name or a key place (keyPlace), but for a git hook (the guard
  // stops those), to what a write through it reaches in the folder that is not protected (linkedInto).
  links: Map<string, string>;
  // Each other link outside a dependency folder, but for a git hook, to the dependency folder in
  // the folder it leads into: it would show what was unpacked there, which no rule judges.
  dependencyLinks: Map<string, string>;
  // Where a paused rebase or cherry-pick keeps its todo, in each git folder the walk found.
  todos: string[];
}

export interface GuardOptions {
  // After a host was killed: the hooks that were the user's before its commands ran.
  inherited?: ReadonlyMap<string, string> | null;
  // Told the baseline once it is known, for the folder's record.
  known?: (hooks: ReadonlyMap<string, string>) => void;
  // Every folder a command can write: a hook linked into one is a command's to change.
  writable?: readonly string[];
  timeoutMs?: number;
  // Whether something of the chat's other than its runs could be writing the folder now: a
  // background process, a runner, or a guest run that was answered before its processes ended.
  writing?: () => boolean;
  // Whether a command of the chat's is running, whose git may be working through a todo: a look
  // leaves the todos alone until none is, so a rebase that finishes within its command keeps its steps.
  running?: () => boolean;
}

// A key under a hooks folder inside a .git folder, at any depth and in any case.
export function isGitHook(folder: string, key: string): boolean {
  if (key === folder || !inside(key, folder)) return false;
  const parts = key.slice(folder.length + 1).toLowerCase().split("/");
  const git = parts.indexOf(".git");
  const hooks = git < 0 ? -1 : parts.indexOf("hooks", git + 1);
  return hooks > git && hooks < parts.length - 1;
}

// Folder-relative names, sorted, at most MAX_LISTED of them.
export function listed(folder: string, paths: readonly string[]): string {
  const names = paths.map((path) => relative(folder, path)).sort();
  return names.length > MAX_LISTED
    ? `${names.slice(0, MAX_LISTED).join(", ")} and ${names.length - MAX_LISTED} more`
    : names.join(", ");
}

// What tells whether a hook changed. Git runs what a link points to, so a link
// is unchanged only while its target is too.
async function describe(path: string): Promise<string | null> {
  const stats = await lstat(path).catch(() => null);
  if (!stats) return null;
  const own = `${stats.ino}:${stats.size}:${stats.mtimeMs}:${stats.ctimeMs}:${stats.mode}`;
  if (!stats.isSymbolicLink()) return own;
  const target = await realpath(path).catch(() => null);
  const pointed = target ? await stat(target).catch(() => null) : null;
  return pointed
    ? `${own}>${target}:${pointed.ino}:${pointed.size}:${pointed.mtimeMs}:${pointed.ctimeMs}:${pointed.mode}`
    : `${own}>`;
}

// Where the guest's rule refuses to move a folder to or from, since what it holds would arrive
// unjudged: a KEY_FOLDERS leaf (.claude, .git), or a place at or under .git/modules or .git/worktrees.
function keyPlace(folder: string, path: string): boolean {
  const parts = path.slice(folder.length + 1).toLowerCase().split("/");
  return KEY_FOLDERS.has(parts.at(-1) ?? "") || parts.some((part, i) => part === ".git" && (parts[i + 1] === "modules" || parts[i + 1] === "worktrees"));
}

// What a write through the link at *path* reaches in the folder that is not protected, or null:
// where it ends, followed fully as a write follows it, or any link on the way there that lies in
// the folder, which a command could swap for a folder of its own. The guest's rule judges a write
// by the path it reaches, not by a link's name on the way.
// ponytail: paths.realpath is synchronous, so a stalled mount on a link's way blocks the host and the
// look's timeout with it; it runs only for links at protected names or key places. An async walk of
// the links would let the timeout fire.
function linkedInto(folder: string, path: string): string | null {
  const links = new Map<string, string | null>();
  const end = followed(path, links);
  const reached = [...(end.loop ? [] : [end.path]), ...[...links.keys()].filter((link) => link !== path)];
  return reached.find((to) => inside(to, folder) && !protectedInFolder(folder, to)) ?? null;
}

// The dependency folder in the folder that the link at *path* leads into, followed fully, or null.
// Synchronous, with linkedInto's ceiling.
function leadsInto(folder: string, path: string): string | null {
  const end = followed(path);
  return end.loop ? null : dependencyFolder(folder, end.path);
}

// Never rejects. Linked folders are not followed; node_modules and git's object
// stores are skipped: they are large, and git runs no hook from them.
export async function scanHooks(folder: string, uid = process.getuid?.() ?? -1): Promise<HookScan> {
  const scan: HookScan = { hooks: new Map(), unreadable: [], links: new Map(), dependencyLinks: new Map(), todos: [] };
  const walk = async (dir: string, inGit: boolean): Promise<void> => {
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      // Gone while the walk went by.
      if (code === "ENOENT" || code === "ENOTDIR") return;
      // Git runs nothing from below a hooks folder, and a hook runs nothing from
      // a folder it cannot search, as below one the guard closed.
      if (isGitHook(folder, dir) && await access(dir, constants.X_OK).then(() => false, () => true)) return;
      // Somebody else's folder the user cannot write (a docker volume) is none of a command's doing.
      const owner = await lstat(dir).then((stats) => stats.uid, () => uid);
      const writable = await access(dir, constants.W_OK).then(() => true, () => false);
      if (owner === uid || writable) scan.unreadable.push(dir);
      return;
    }
    // A git folder holds HEAD. Its objects/ is the object store only when it holds
    // no HEAD of its own: else it may be the git folder of a submodule named objects.
    const gitFolder = inGit && entries.some((entry) => entry.name === "HEAD");
    // A .git folder, or a git folder in one: a submodule's, a linked worktree's.
    if (gitFolder || basename(dir).toLowerCase() === ".git") scan.todos.push(join(dir, "rebase-merge", "git-rebase-todo"), join(dir, "sequencer", "todo"));
    await Promise.all(entries.map(async (entry) => {
      const path = join(dir, entry.name);
      // A name that is not valid UTF-8 reads back with U+FFFD, and no path reaches it.
      if (entry.name.includes("\uFFFD")) {
        scan.unreadable.push(path);
        return;
      }
      const name = entry.name.toLowerCase();
      const key = !(entry.isDirectory() && name === ".git") && protectedInFolder(folder, path);
      const to = entry.isSymbolicLink() && (key || keyPlace(folder, path)) && !isGitHook(folder, path) ? linkedInto(folder, path) : null;
      if (to !== null) scan.links.set(path, to);
      else if (entry.isSymbolicLink() && !isGitHook(folder, path) && dependencyFolder(folder, path) === null) {
        const into = leadsInto(folder, path);
        if (into !== null) scan.dependencyLinks.set(path, into);
      }
      if (entry.isDirectory()) {
        const store = gitFolder && name === "objects"
          && await lstat(join(path, "HEAD")).then(() => false, (error: NodeJS.ErrnoException) => error.code === "ENOENT");
        if (name === "node_modules" || store) return;
        await walk(path, inGit || name === ".git");
      } else if ((entry.isFile() || entry.isSymbolicLink()) && isGitHook(folder, path)) {
        const signature = await describe(path);
        if (signature) scan.hooks.set(path, signature);
      }
    }));
  };
  await walk(folder, false);
  return scan;
}

// Changes one file's mode through a handle on it, never by its path again: a
// command still running could swap a folder on the path for a link out of the
// folder between a look and a chmod. Done only when the opened file is a folder
// (*directory*) or a file no other path shares, and really lies inside *folders*.
export async function chmodInside(
  path: string, folders: readonly string[], change: (mode: number) => number, directory = false,
): Promise<boolean> {
  // O_NOCTTY: the host leads a session with no terminal, and a path swapped for one must not become its own.
  const flags = constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW | constants.O_NOCTTY
    | (directory ? constants.O_DIRECTORY : 0);
  const handle = await open(path, flags).catch(() => null);
  if (!handle) return false;
  try {
    const stats = await handle.stat();
    // A file hard-linked elsewhere is the same file at its other paths, which may be outside.
    if (directory ? !stats.isDirectory() : !stats.isFile() || stats.nlink !== 1) return false;
    const real = await readlink(`/proc/self/fd/${handle.fd}`);
    // Unlinked since the open: its one link left may be outside. A file really named so is refused too.
    if (real.endsWith(" (deleted)") || !folders.some((dir) => inside(real, dir))) return false;
    await handle.chmod(change(stats.mode));
    return true;
  } catch {
    return false;
  } finally {
    await handle.close().catch(() => {});
  }
}

// Makes every hook that is not in *baseline* unchanged unable to run: the keys
// it changed, and the keys it could not stop.
export async function neutralize(
  folder: string, scan: HookScan, baseline: ReadonlyMap<string, string>, writable: readonly string[] = [folder],
): Promise<{ changed: string[]; stuck: string[] }> {
  const changed: string[] = [];
  const stuck: string[] = [];
  for (const [key, signature] of scan.hooks) {
    if (baseline.get(key) === signature || key.toLowerCase().endsWith(".sample")) continue;
    // Git runs what a link points to.
    const target = await realpath(key).catch(() => null);
    const stats = target ? await stat(target).catch(() => null) : null;
    if (!target || !stats?.isFile() || (stats.mode & 0o111) === 0) continue;
    if (await chmodInside(target, writable, (mode) => mode & 0o7666)) {
      changed.push(key);
      continue;
    }
    // A program no command wrote, which git would still run with arguments from
    // the repository, or a file that could not be changed: git reaches no hook in
    // a hooks folder it cannot search.
    const closed = await chmodInside(dirname(key), [folder], (mode) => mode & 0o7666, true);
    (closed ? changed : stuck).push(key);
  }
  return { changed, stuck };
}

// What git reads at *path*, through links: null when there is none; undefined when git could read
// something this does not: a FIFO or a device it would wait on or read without end, or a file too large.
async function readTodo(path: string): Promise<{ text: string; stats: Stats } | null | undefined> {
  const file = await open(path, constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOCTTY)
    .catch((error: NodeJS.ErrnoException) => (error.code === "ENOENT" || error.code === "ENOTDIR" ? null : undefined));
  if (!file) return file;
  try {
    const stats = await file.stat();
    // Byte for byte: a line this leaves is written back as it was.
    return stats.isFile() && stats.size <= MAX_TODO ? { text: await file.readFile("latin1"), stats } : undefined;
  } catch {
    return undefined;
  } finally {
    await file.close().catch(() => {});
  }
}

// The exec steps in the todo at *path*, as they are now.
async function todoSteps(path: string): Promise<Set<string>> {
  const read = await readTodo(path);
  return new Set(read ? read.text.split("\n").filter((line) => EXEC.test(line)) : []);
}

// Comments out each exec step in the todo at *path* that is not in *keep*. The todo is replaced
// whole, as git replaces it, so a git reading it meanwhile reads one or the other; and only through
// a handle on its folder, where that really lies inside *folders*, as chmodInside changes a mode. A
// todo linked to a file becomes a file, and the file is left. "stuck": there are steps to comment
// out and this could not, or git could read a todo there that this cannot.
async function stripTodo(path: string, keep: ReadonlySet<string>, folders: readonly string[]): Promise<"none" | "stripped" | "stuck"> {
  const dir = await open(dirname(path), constants.O_RDONLY | constants.O_DIRECTORY).catch(() => null);
  if (!dir) return "none";
  const at = `/proc/self/fd/${dir.fd}`;
  const todo = join(at, basename(path));
  const temp = join(at, `.${basename(path)}.${randomUUID()}`);
  let made = false;
  try {
    const read = await readTodo(todo);
    if (!read) return read === null ? "none" : "stuck";
    const lines = read.text.split("\n");
    const added = new Set(lines.filter((line) => EXEC.test(line) && !keep.has(line)));
    if (added.size === 0) return "none";
    const real = await readlink(at);
    if (real.endsWith(" (deleted)") || !folders.some((folder) => inside(real, folder))) return "stuck";
    const out = await open(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, read.stats.mode & 0o666);
    made = true;
    try {
      await out.writeFile(lines.map((line) => (added.has(line) ? `${REMOVED}${line}` : line)).join("\n"), "latin1");
    } finally {
      await out.close();
    }
    // Changed since it was read, as by a git going on: judged again at the next look.
    // ponytail: a write between this and the rename is lost; taking git's own lock file would close that.
    const now = await stat(todo);
    if (now.ino !== read.stats.ino || now.dev !== read.stats.dev || now.size !== read.stats.size || now.mtimeMs !== read.stats.mtimeMs) return "stuck";
    await rename(temp, todo);
    made = false;
    return "stripped";
  } catch {
    return "stuck";
  } finally {
    if (made) await unlink(temp).catch(() => {});
    await dir.close().catch(() => {});
  }
}

export class HookGuard {
  // The user's own hooks, as they were: null until a look has seen the whole folder.
  private baseline: ReadonlyMap<string, string> | null;
  private recorded = false;
  private blocked: string | null = null;
  private looks = 0;
  // Hooks a look between commands stopped, for the next command's output.
  private readonly unreported = new Set<string>();
  // The exec steps in each todo when nothing of the chat's could last write there: the user's.
  // null until first recorded. wrote: something of the chat's may have written since. The todos
  // the latest look found, and those a look between commands commented steps out of, for the
  // next command's output. turn: the todo work of looks and refusals, one at a time, so a record
  // never lands between another look's strip and its own.
  private steps: Map<string, Set<string>> | null;
  private wrote: boolean;
  private turn: Promise<unknown> = Promise.resolve();
  private todos: string[] = [];
  private readonly unstripped = new Set<string>();
  private readonly first: Promise<unknown>;
  private readonly known: (hooks: ReadonlyMap<string, string>) => void;
  private readonly writable: readonly string[];
  private readonly timeoutMs: number;
  private readonly writing: () => boolean;
  private readonly running: () => boolean;

  constructor(private readonly folder: string, options: GuardOptions = {}) {
    this.baseline = options.inherited ?? null;
    this.known = options.known ?? (() => {});
    this.writable = options.writable ?? [folder];
    this.timeoutMs = options.timeoutMs ?? SCAN_TIMEOUT_MS;
    this.writing = options.writing ?? (() => false);
    this.running = options.running ?? (() => false);
    // After a crash: no step in a todo is known to be the user's, since the killed host's commands ran.
    this.steps = options.inherited ? new Map() : null;
    this.wrote = Boolean(options.inherited);
    // The first look starts at once, while the helper starts. After a crash it also
    // catches what the killed host's commands left.
    this.first = this.check();
  }

  // Why the next command may not run, or null: the last look did not see the
  // whole folder, or left a hook it could not stop. A command that may run starts
  // here: the exec steps in the todos now are the user's, as a rebase -x of theirs
  // left them, unless something of the chat's could have written there since the
  // last record, which then stays, so the next look comments those out.
  async refusal(): Promise<Outcome | null> {
    await this.first;
    if (this.blocked) await this.check();
    if (this.blocked) return { error: { type: "sandbox", message: this.blocked } };
    await this.serial(async () => {
      if (!this.steps || (!this.wrote && !this.writing())) await this.record();
      this.wrote = true;
    });
    return null;
  }

  // After a command, whatever its outcome: its hooks are made non-executable, and its output says so.
  async after(outcome: Outcome): Promise<Outcome> {
    await this.first;
    const { changed, stripped } = await this.check();
    if (!("ok" in outcome)) {
      // Told with the next command's output.
      for (const key of changed) this.unreported.add(key);
      for (const todo of stripped) this.unstripped.add(todo);
      return outcome;
    }
    const told = [...new Set([...this.unreported, ...changed])];
    const removed = [...new Set([...this.unstripped, ...stripped])];
    this.unreported.clear();
    this.unstripped.clear();
    const notices = [
      told.length > 0 ? `${HOOKS_NOTICE}${listed(this.folder, told)}` : null,
      removed.length > 0 ? `${STEPS_NOTICE}${listed(this.folder, removed)}` : null,
    ].filter(Boolean);
    if (notices.length === 0) return outcome;
    const ok = outcome.ok as { output: string; returncode: number; timed_out: boolean };
    return { ok: { ...ok, output: `${ok.output}${ok.output ? "\n" : ""}${notices.join("\n")}` } };
  }

  // A look between commands, once one has run in the guest: what it left running may
  // write a hook at any time. What it stops is told with the next command's output.
  async watch(): Promise<void> {
    await this.first;
    const { changed, stripped } = await this.check();
    for (const key of changed) this.unreported.add(key);
    for (const todo of stripped) this.unstripped.add(todo);
  }

  // The last look, when the host stops: what a stopped command left. False when
  // it could not see the whole folder, left a hook it could not stop, or left the
  // todos alone for a run still in flight, so the record must not say the host
  // stopped cleanly: the next host then takes no exec step there as the user's.
  async settle(): Promise<boolean> {
    await this.first;
    const { blocked, held } = await this.check();
    return !blocked && !this.blocked && !held;
  }

  // One look, numbered. Only the newest says whether commands may run: an older
  // one may have seen the folder before the newest did. Its own verdict goes back
  // to the caller either way.
  private async check(): Promise<{ changed: string[]; stripped: string[]; held: boolean; blocked: string | null }> {
    const mine = ++this.looks;
    const verdict = await this.look();
    if (mine === this.looks) this.blocked = verdict.blocked;
    return verdict;
  }

  // The first look that sees the whole folder sets the baseline, which is
  // recorded before any command runs; every look makes what is not in it unable
  // to run, as far as it can see, and, while no command runs, comments out each exec step in a
  // todo that is not the user's. What it changed, the todos it commented steps out of, and why commands may not run, or null.
  private async look(): Promise<{ changed: string[]; stripped: string[]; held: boolean; blocked: string | null }> {
    let timer: NodeJS.Timeout | undefined;
    const late = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), this.timeoutMs);
    });
    const scan = await Promise.race([scanHooks(this.folder), late]);
    clearTimeout(timer);
    const unseen = !scan
      ? `Blocked: the computer could not look through this folder for git hooks within ${this.timeoutMs / 1000} seconds, so commands cannot run here.`
      : scan.unreadable.length > 0
        ? `Blocked: the computer cannot read ${listed(this.folder, scan.unreadable)} in this folder, so it cannot check there for git hooks, which would run outside the sandbox. Make it readable to run commands here.`
        : null;
    if (!scan || (unseen && !this.baseline)) return { changed: [], stripped: [], held: true, blocked: unseen };
    this.baseline ??= scan.hooks;
    if (!this.recorded) {
      try {
        this.known(this.baseline);
        this.recorded = true;
      } catch (error) {
        return {
          changed: [],
          stripped: [],
          held: true,
          blocked: `Blocked: the computer could not record this folder's state, so commands cannot run here: ${error instanceof Error ? error.message : String(error)}`,
        };
      }
    }
    const { changed, stuck } = await neutralize(this.folder, scan, this.baseline, this.writable);
    this.todos = scan.todos;
    const { stripped, stuck: unstrippable, held } = await this.todosLook(scan.todos);
    const unstopped = stuck.length > 0
      ? `Blocked: the computer could not stop these git hooks from running outside the sandbox: ${listed(this.folder, stuck)}. Remove them or make them non-executable to run commands here.`
      : null;
    const links = [...scan.links].map(([path, to]) => `${relative(this.folder, path)} is a link to ${relative(this.folder, to) || "."} in this folder.`).sort();
    const linked = links.length > 0
      ? `Blocked: ${links.join(" ")} Make ${links.length > 1 ? "each" : "it"} a file or folder of its own, or point it outside the folder or at a protected name, to run commands here.`
      : null;
    const intoDependencies = [...scan.dependencyLinks].map(([path, to]) => `${relative(this.folder, path)} leads into ${relative(this.folder, to)}.`).sort();
    const shown = intoDependencies.length > 0
      ? `Blocked: ${intoDependencies.join(" ")} Remove the link${intoDependencies.length > 1 ? "s" : ""} to run commands here.`
      : null;
    const unremoved = unstrippable.length > 0
      ? `Blocked: the computer could not remove the steps that appeared in ${listed(this.folder, unstrippable)} while this chat's commands could write there, which git would run outside the sandbox. Abort that rebase or cherry-pick, or remove those exec lines, to run commands here.`
      : null;
    return { changed, stripped, held, blocked: [unstopped, unseen, linked, shown, unremoved].filter(Boolean).join(" ") || null };
  }

  // A look's work on *todos*. While a run is in flight it does nothing, held: the run's own git
  // may be working through one. Otherwise, once something of the chat's may have written since
  // the record, it comments out each exec step not in it; and while nothing of the chat's can
  // write, what is left is the user's, as a rebase -x of theirs paused meanwhile left it.
  private todosLook(todos: readonly string[]): Promise<{ stripped: string[]; stuck: string[]; held: boolean }> {
    return this.serial(async () => {
      const stripped: string[] = [];
      const stuck: string[] = [];
      if (this.running()) return { stripped, stuck, held: true };
      const steps = this.steps;
      if (steps && this.wrote) {
        for (const todo of todos) {
          const result = await stripTodo(todo, steps.get(todo) ?? new Set(), this.writable);
          if (result === "stripped") stripped.push(todo);
          else if (result === "stuck") stuck.push(todo);
        }
      }
      // One it could not strip stays the chat's, to be judged again.
      if (!this.writing() && stuck.length === 0) {
        await this.record();
        this.wrote = false;
      }
      return { stripped, stuck, held: false };
    });
  }

  // The exec steps in the latest look's todos are the user's.
  private async record(): Promise<void> {
    this.steps = new Map(await Promise.all(this.todos.map(async (todo) => [todo, await todoSteps(todo)] as const)));
  }

  private serial<T>(work: () => Promise<T>): Promise<T> {
    const turn = this.turn.then(work);
    this.turn = turn.catch(() => {});
    return turn;
  }
}

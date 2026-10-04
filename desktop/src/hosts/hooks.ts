// Git hooks a command left in the folder. Git runs an executable file in a .git
// folder's hooks folder outside the sandbox, the next time someone runs git there,
// and srt protects a repository's hooks only when it was there before the command
// started. So after every command the host looks through the whole folder and
// makes each hook that is not the user's own, unchanged, non-executable: git skips
// those. Nothing is deleted.

import { access, constants, lstat, open, readdir, readlink, realpath, stat } from "node:fs/promises";
import { dirname, join, relative } from "node:path";

import { inside } from "../files/paths.js";
import type { Outcome } from "../link/protocol.js";

export const SCAN_TIMEOUT_MS = 30_000;
export const HOOKS_NOTICE = "The computer stopped these git hooks from running, because git would run them outside the sandbox (where it closed a hooks folder, that repository's other hooks are off until you make it searchable again): ";
export const MAX_LISTED = 20;

export interface HookScan {
  // Each hook's key, and what tells whether it changed (see describe).
  hooks: Map<string, string>;
  // Folders that could not be read where a command could have hidden a hook.
  unreadable: string[];
}

export interface GuardOptions {
  // After a host was killed: the hooks that were the user's before its commands ran.
  inherited?: ReadonlyMap<string, string> | null;
  // Told the baseline once it is known, for the folder's record.
  known?: (hooks: ReadonlyMap<string, string>) => void;
  // Every folder a command can write: a hook linked into one is a command's to change.
  writable?: readonly string[];
  timeoutMs?: number;
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
function listed(folder: string, paths: readonly string[]): string {
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

// Never rejects. Linked folders are not followed; node_modules and git's object
// stores are skipped: they are large, and git runs no hook from them.
export async function scanHooks(folder: string, uid = process.getuid?.() ?? -1): Promise<HookScan> {
  const scan: HookScan = { hooks: new Map(), unreadable: [] };
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
    await Promise.all(entries.map(async (entry) => {
      const path = join(dir, entry.name);
      const name = entry.name.toLowerCase();
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

export class HookGuard {
  // The user's own hooks, as they were: null until a look has seen the whole folder.
  private baseline: ReadonlyMap<string, string> | null;
  private recorded = false;
  private blocked: string | null = null;
  private looks = 0;
  private readonly first: Promise<unknown>;
  private readonly known: (hooks: ReadonlyMap<string, string>) => void;
  private readonly writable: readonly string[];
  private readonly timeoutMs: number;

  constructor(private readonly folder: string, options: GuardOptions = {}) {
    this.baseline = options.inherited ?? null;
    this.known = options.known ?? (() => {});
    this.writable = options.writable ?? [folder];
    this.timeoutMs = options.timeoutMs ?? SCAN_TIMEOUT_MS;
    // The first look starts at once, while srt starts. After a crash it also
    // catches what the killed host's commands left.
    this.first = this.check();
  }

  // Why the next command may not run, or null: the last look did not see the
  // whole folder, or left a hook it could not stop.
  async refusal(): Promise<Outcome | null> {
    await this.first;
    if (this.blocked) await this.check();
    return this.blocked ? { error: { type: "sandbox", message: this.blocked } } : null;
  }

  // After a command, whatever its outcome: its hooks are made non-executable, and its output says so.
  async after(outcome: Outcome): Promise<Outcome> {
    await this.first;
    const { changed } = await this.check();
    if (changed.length === 0 || !("ok" in outcome)) return outcome;
    const ok = outcome.ok as { output: string; returncode: number; timed_out: boolean };
    return { ok: { ...ok, output: `${ok.output}${ok.output ? "\n" : ""}${HOOKS_NOTICE}${listed(this.folder, changed)}` } };
  }

  // The last look, when the host stops: what a stopped command left. False when
  // it could not see the whole folder or left a hook it could not stop, so the
  // record must not say the host stopped cleanly.
  async settle(): Promise<boolean> {
    await this.first;
    const { blocked } = await this.check();
    return !blocked && !this.blocked;
  }

  // One look, numbered. Only the newest says whether commands may run: an older
  // one may have seen the folder before the newest did. Its own verdict goes back
  // to the caller either way.
  private async check(): Promise<{ changed: string[]; blocked: string | null }> {
    const mine = ++this.looks;
    const verdict = await this.look();
    if (mine === this.looks) this.blocked = verdict.blocked;
    return verdict;
  }

  // The first look that sees the whole folder sets the baseline, which is
  // recorded before any command runs; every look makes what is not in it unable
  // to run, as far as it can see. What it changed, and why commands may not run, or null.
  private async look(): Promise<{ changed: string[]; blocked: string | null }> {
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
    if (!scan || (unseen && !this.baseline)) return { changed: [], blocked: unseen };
    this.baseline ??= scan.hooks;
    if (!this.recorded) {
      try {
        this.known(this.baseline);
        this.recorded = true;
      } catch (error) {
        return {
          changed: [],
          blocked: `Blocked: the computer could not record this folder's state, so commands cannot run here: ${error instanceof Error ? error.message : String(error)}`,
        };
      }
    }
    const { changed, stuck } = await neutralize(this.folder, scan, this.baseline, this.writable);
    const unstopped = stuck.length > 0
      ? `Blocked: the computer could not stop these git hooks from running outside the sandbox: ${listed(this.folder, stuck)}. Remove them or make them non-executable to run commands here.`
      : null;
    return { changed, blocked: [unstopped, unseen].filter(Boolean).join(" ") || null };
  }
}

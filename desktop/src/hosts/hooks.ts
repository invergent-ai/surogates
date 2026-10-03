// Git hooks a command left in the folder. Git runs an executable file in a .git
// folder's hooks folder outside the sandbox, the next time someone runs git there,
// and srt protects a repository's hooks only when it was there before the command
// started. So after every command the host looks through the whole folder and
// makes each hook that is not the user's own, unchanged, non-executable: git skips
// those. Nothing is deleted.

import { access, chmod, constants, lstat, readdir, realpath, stat } from "node:fs/promises";
import { dirname, join, relative } from "node:path";

import { inside } from "../files/paths.js";
import type { Outcome } from "../link/protocol.js";

export const SCAN_TIMEOUT_MS = 30_000;
export const HOOKS_NOTICE = "The computer made these git hooks non-executable, because git would run them outside the sandbox: ";
const MAX_LISTED = 20;

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
      // Somebody else's folder the user cannot write (a docker volume) is none of a command's doing.
      const owner = await lstat(dir).then((stats) => stats.uid, () => uid);
      const writable = await access(dir, constants.W_OK).then(() => true, () => false);
      if (owner === uid || writable) scan.unreadable.push(dir);
      return;
    }
    // A git folder holds HEAD; its objects/ is where a submodule's would not be.
    const gitFolder = inGit && entries.some((entry) => entry.name === "HEAD");
    await Promise.all(entries.map(async (entry) => {
      const path = join(dir, entry.name);
      const name = entry.name.toLowerCase();
      if (entry.isDirectory()) {
        if (name === "node_modules" || (gitFolder && name === "objects")) return;
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

// Makes every hook that is not in *baseline* unchanged unable to run; the keys it changed.
export async function neutralize(
  folder: string, scan: HookScan, baseline: ReadonlyMap<string, string>, writable: readonly string[] = [folder],
): Promise<string[]> {
  const changed: string[] = [];
  for (const [key, signature] of scan.hooks) {
    if (baseline.get(key) === signature || key.toLowerCase().endsWith(".sample")) continue;
    // Git runs what a link points to.
    const target = await realpath(key).catch(() => null);
    const stats = target ? await stat(target).catch(() => null) : null;
    if (!target || !stats?.isFile() || (stats.mode & 0o111) === 0) continue;
    if (writable.some((dir) => inside(target, dir))) {
      await chmod(target, stats.mode & 0o7666).then(() => changed.push(key), () => {});
      continue;
    }
    // A program no command wrote, which git would still run with arguments from
    // the repository: git reaches no hook in a hooks folder it cannot search.
    const hooks = dirname(key);
    const folderStats = await stat(hooks).catch(() => null);
    if (folderStats) await chmod(hooks, folderStats.mode & 0o7666).then(() => changed.push(key), () => {});
  }
  return changed;
}

export class HookGuard {
  // The user's own hooks, as they were: null until a look has seen the whole folder.
  private baseline: ReadonlyMap<string, string> | null;
  private recorded = false;
  private blocked: string | null = null;
  private readonly first: Promise<string[]>;
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

  // Why the next command may not run, or null: the last look did not see the whole folder.
  async refusal(): Promise<Outcome | null> {
    await this.first;
    if (this.blocked) await this.check();
    return this.blocked ? { error: { type: "sandbox", message: this.blocked } } : null;
  }

  // After a command, whatever its outcome: its hooks are made non-executable, and its output says so.
  async after(outcome: Outcome): Promise<Outcome> {
    await this.first;
    const changed = await this.check();
    if (changed.length === 0 || !("ok" in outcome)) return outcome;
    const ok = outcome.ok as { output: string; returncode: number; timed_out: boolean };
    const names = changed.map((key) => relative(this.folder, key)).sort();
    const listed = names.length > MAX_LISTED
      ? `${names.slice(0, MAX_LISTED).join(", ")} and ${names.length - MAX_LISTED} more`
      : names.join(", ");
    return { ok: { ...ok, output: `${ok.output}${ok.output ? "\n" : ""}${HOOKS_NOTICE}${listed}` } };
  }

  // The last look, when the host stops: what a stopped command left. False when
  // it could not see the whole folder, so the record must not say the host stopped cleanly.
  async settle(): Promise<boolean> {
    await this.first;
    await this.check();
    return !this.blocked;
  }

  // One look. The first that sees the whole folder sets the baseline, which is
  // recorded before any command runs; every look makes what is not in it unable
  // to run, as far as it can see.
  private async check(): Promise<string[]> {
    const scan = await this.look();
    if (!scan) return [];
    if (!this.baseline) {
      if (this.blocked) return [];
      this.baseline = scan.hooks;
    }
    if (!this.recorded) {
      try {
        this.known(this.baseline);
        this.recorded = true;
      } catch (error) {
        this.blocked = `Blocked: the computer could not record this folder's state, so commands cannot run here: ${error instanceof Error ? error.message : String(error)}`;
        return [];
      }
    }
    return neutralize(this.folder, scan, this.baseline, this.writable);
  }

  private async look(): Promise<HookScan | null> {
    let timer: NodeJS.Timeout | undefined;
    const late = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), this.timeoutMs);
    });
    const scan = await Promise.race([scanHooks(this.folder), late]);
    clearTimeout(timer);
    if (!scan) {
      this.blocked = `Blocked: the computer could not look through this folder for git hooks within ${this.timeoutMs / 1000} seconds, so commands cannot run here.`;
    } else if (scan.unreadable.length > 0) {
      const names = scan.unreadable.map((dir) => relative(this.folder, dir)).sort().join(", ");
      this.blocked = `Blocked: the computer cannot read ${names} in this folder, so it cannot check there for git hooks, which would run outside the sandbox. Make it readable to run commands here.`;
    } else {
      this.blocked = null;
    }
    return scan;
  }
}

// What a root's session runner needs to be restarted: the protected keys in the
// folder, the denies srt's own rules miss, and why it restarted. srt fixes a
// runner's mounts when it is wrapped, so a protected path that appears later is
// writable inside it until the host wraps a new one (spec, Section 1).

import { lstatSync } from "node:fs";
import { join, relative } from "node:path";

import { sandboxError } from "../files/answers.js";
import { GIT_STATE, KEY_FOLDERS, protectedInFolder } from "../files/protect.js";
import { type BindMode, MAX_PROTECTED } from "../guest/protocol.js";
import { SCAN_TIMEOUT_MS, listed, scanHooks } from "./hooks.js";
import { GLOB } from "./policy.js";

// bwrap takes 9 000 arguments, and 2 905 literals fit beside srt's own rules and scan
// here. A parent folder of about 130 checkouts already needs 256.
export const MAX_EXTRA_DENIES = 1024;

export const GRANT_CHANGED = "a folder grant changed";

export const appeared = (key: string) => `a protected file appeared in the folder (${key})`;

// srt 0.0.77's own write denies (linux-sandbox-utils.js) that hold whatever happens:
// these names at the folder's top, and .git's hooks and config there when .git is a
// folder. Its nested scan is not credited: srt drops it silently when rg fails, as
// it does on any folder it cannot read.
const SRT_TOP = new Set([
  ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile", ".ripgreprc", ".mcp.json",
  ".vscode", ".idea", ".claude/commands", ".claude/agents",
]);

// srt's own deny targets at the folder's top, .git's hooks and config among them
// (denied only when .git is a folder). One absent at the wrap is watched for appearing.
export const srtTargets = (folder: string): string[] => [...SRT_TOP, ".git/hooks", ".git/config"].map((name) => join(folder, name));

// A deny target as the runner's wrap held it, or null when absent. srt's mounts hold the
// inode the path had then: one renamed over or made again from outside is writable inside.
export function identity(path: string): string | null {
  try {
    const stats = lstatSync(path);
    return `${stats.dev}:${stats.ino}`;
  } catch {
    return null;
  }
}

// The folder's protected keys, from one walk. A walk that takes too long, or that
// could not read a folder, fails closed: a key hidden there would get no deny.
export async function protectedKeys(folder: string, timeoutMs = SCAN_TIMEOUT_MS): Promise<Set<string>> {
  let timer: NodeJS.Timeout | undefined;
  const late = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), timeoutMs);
  });
  const scan = await Promise.race([scanHooks(folder), late]);
  clearTimeout(timer);
  if (!scan) {
    throw sandboxError(`Blocked: the computer could not look through this folder within ${timeoutMs / 1000} seconds, so background processes cannot start here.`);
  }
  if (scan.unreadable.length > 0) {
    throw sandboxError(`Blocked: the computer cannot read ${listed(folder, scan.unreadable)} in this folder, so it cannot tell which paths to protect there, and background processes cannot start here. Make it readable to start them.`);
  }
  return scan.protectedKeys;
}

// The paths a runner's wrap denies writes to beyond srt's own: for every protected
// key, the outermost protected path it lies in, unless srt already denies it.
export function extraDenies(folder: string, keys: Iterable<string>): string[] {
  let gitFolder: boolean | undefined;
  const topGit = () => (gitFolder ??= lstatSync(join(folder, ".git"), { throwIfNoEntry: false })?.isDirectory() === true);
  const denies = new Set<string>();
  for (const key of keys) {
    const path = outermost(folder, key.slice(folder.length + 1).split("/")).join("/");
    // Spelled as srt spells them: it matches these in this case only.
    const srts = SRT_TOP.has(path) || ((path === ".git/hooks" || path === ".git/config") && topGit());
    if (!srts) denies.add(join(folder, path));
  }
  if (denies.size > MAX_EXTRA_DENIES) {
    throw sandboxError(`Blocked: this folder has ${denies.size} protected paths the sandbox does not cover by itself, and the computer can protect at most ${MAX_EXTRA_DENIES}, so background processes cannot start here.`);
  }
  const sorted = [...denies].sort();
  // srt drops a denyWrite path that holds a glob character, without a word.
  const globbed = sorted.find((path) => GLOB.test(path));
  if (globbed) {
    throw sandboxError(`Blocked: this computer cannot protect ${relative(folder, globbed)} in the sandbox, because its name holds *, ?, [ or ], so background processes cannot start here.`);
  }
  return sorted;
}

// What a guest root's namespace binds over itself (spec, Section 11, Folders), sorted, so
// each folder comes before what lies in it: for every protected key the outermost protected
// path it lies in, read-only; and, read-write, each folder above it that holds protected
// names (KEY_FOLDERS) or lies in a .git folder. A command can then neither write a key in
// place nor rename such a folder to make the key again. An ordinary folder above a key is
// not bound: it stays the agent's to rename. Nor is git's own working state (GIT_STATE: a
// rebase's, a cherry-pick's, a linked worktree's), which git makes, writes and removes as it
// works, across commands too: bound, a rebase would stop halfway and could neither go on nor
// be aborted. So what is held in a .git is the .git itself, and in its modules each folder down
// to a submodule's git folder, all of which git keeps. Past MAX_PROTECTED it throws, and commands are
// refused, as srt's extra denies were past their own: each is a bind in the root's
// namespace, and its path goes over the control port.
export function guestBinds(folder: string, keys: Iterable<string>): Array<[path: string, mode: BindMode]> {
  const binds = new Map<string, BindMode>();
  for (const key of keys) {
    const parts = outermost(folder, key.slice(folder.length + 1).split("/"));
    const lower = parts.map((part) => part.toLowerCase());
    if (GIT_STATE.has(lower.at(-1) ?? "") && lower.slice(0, -1).includes(".git")) continue;
    binds.set(join(folder, ...parts), "ro");
    for (let at = 1; at < parts.length; at += 1) {
      const above = lower.slice(0, at);
      if (KEY_FOLDERS.has(above.at(-1) ?? "") || above.slice(0, -1).includes(".git")) {
        const path = join(folder, ...parts.slice(0, at));
        if (!binds.has(path)) binds.set(path, "rw");
      }
    }
  }
  if (binds.size > MAX_PROTECTED) {
    throw new Error(`this folder has ${binds.size} protected paths, and the sandbox can make at most ${MAX_PROTECTED} read-only`);
  }
  return [...binds].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}

// .git/hooks for a hook, .vscode for a file in it. Never a .git folder, which would
// stop git there: its config, hooks and state are what runs code.
function outermost(folder: string, parts: readonly string[]): string[] {
  for (let i = 1; i < parts.length; i += 1) {
    if (parts[i - 1]?.toLowerCase() === ".git") continue;
    if (protectedInFolder(folder, join(folder, ...parts.slice(0, i)))) return parts.slice(0, i);
  }
  return [...parts];
}

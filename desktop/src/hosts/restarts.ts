// What a root's session runner needs to be restarted: the protected keys in the
// folder, the denies srt's own rules miss, and why it restarted. srt fixes a
// runner's mounts when it is wrapped, so a protected path that appears later is
// writable inside it until the host wraps a new one (spec, Section 1).

import { lstatSync } from "node:fs";
import { join } from "node:path";

import { sandboxError } from "../files/answers.js";
import { protectedInFolder } from "../files/protect.js";
import { SCAN_TIMEOUT_MS, listed, scanHooks } from "./hooks.js";
import { SCAN_DEPTH } from "./policy.js";

// bwrap takes 9 000 arguments, about 3 000 mounts, and srt's own rules and scan use some of them.
export const MAX_EXTRA_DENIES = 256;

// srt 0.0.77's own write denies (linux-sandbox-utils.js): these names at the folder's
// top, whatever they are, and .git's hooks and config there when .git is a folder.
// Its scan adds nested files with one of SRT_FILES' names, or a repository's
// config, SCAN_DEPTH deep at most and outside any .git, .vscode or .idea.
const SRT_FILES = new Set([".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile", ".ripgreprc", ".mcp.json"]);
const SRT_TOP = new Set([...SRT_FILES, ".vscode", ".idea", ".claude/commands", ".claude/agents"]);
const SRT_DIRS = new Set([".git", ".vscode", ".idea"]);

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
  const denies = new Set<string>();
  for (const key of keys) {
    const parts = outermost(folder, key.slice(folder.length + 1).split("/"));
    if (!srtDenies(folder, parts)) denies.add(join(folder, ...parts));
  }
  if (denies.size > MAX_EXTRA_DENIES) {
    throw sandboxError(`Blocked: this folder has ${denies.size} protected paths the sandbox does not cover by itself, and the computer can protect at most ${MAX_EXTRA_DENIES}, so background processes cannot start here.`);
  }
  return [...denies].sort();
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

// Whether srt denies writes to this path itself. Spelled as srt spells it: it
// matches .git/config and the top's names in this case only.
function srtDenies(folder: string, parts: readonly string[]): boolean {
  const path = parts.join("/");
  if (SRT_TOP.has(path)) return true;
  if (path === ".git/hooks" || path === ".git/config") return isKind(join(folder, ".git"), "directory");
  // Its scan finds files only, and leaves out a match inside one of its folders but the last.
  if (parts.length > SCAN_DEPTH || !isKind(join(folder, path), "file")) return false;
  const lower = parts.map((part) => part.toLowerCase());
  const name = lower.at(-1) ?? "";
  const above = lower.slice(0, -1);
  if (SRT_FILES.has(name)) return !above.some((part) => SRT_DIRS.has(part));
  return parts.at(-1) === "config" && parts.at(-2) === ".git" && !above.slice(0, -1).some((part) => SRT_DIRS.has(part));
}

function isKind(path: string, kind: "file" | "directory"): boolean {
  const stats = lstatSync(path, { throwIfNoEntry: false });
  return kind === "file" ? stats?.isFile() === true : stats?.isDirectory() === true;
}

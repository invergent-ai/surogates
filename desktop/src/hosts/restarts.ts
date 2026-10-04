// What a root's session runner needs to be restarted: the protected keys in the
// folder, the denies srt's own rules miss, and why it restarted. srt fixes a
// runner's mounts when it is wrapped, so a protected path that appears later is
// writable inside it until the host wraps a new one (spec, Section 1).

import { lstatSync } from "node:fs";
import { join, relative } from "node:path";

import { sandboxError } from "../files/answers.js";
import { protectedInFolder } from "../files/protect.js";
import { SCAN_TIMEOUT_MS, listed, scanHooks } from "./hooks.js";
import { GLOB } from "./policy.js";

// bwrap takes 9 000 arguments, and 2 905 literals fit beside srt's own rules and scan
// here. A parent folder of about 130 checkouts already needs 256.
export const MAX_EXTRA_DENIES = 1024;

// srt 0.0.77's own write denies (linux-sandbox-utils.js) that hold whatever happens:
// these names at the folder's top, and .git's hooks and config there when .git is a
// folder. Its nested scan is not credited: srt drops it silently when rg fails, as
// it does on any folder it cannot read.
const SRT_TOP = new Set([
  ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile", ".ripgreprc", ".mcp.json",
  ".vscode", ".idea", ".claude/commands", ".claude/agents",
]);

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

// .git/hooks for a hook, .vscode for a file in it. Never a .git folder, which would
// stop git there: its config, hooks and state are what runs code.
function outermost(folder: string, parts: readonly string[]): string[] {
  for (let i = 1; i < parts.length; i += 1) {
    if (parts[i - 1]?.toLowerCase() === ".git") continue;
    if (protectedInFolder(folder, join(folder, ...parts.slice(0, i)))) return parts.slice(0, i);
  }
  return [...parts];
}

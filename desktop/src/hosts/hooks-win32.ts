// The hook guard's look on Windows (hooks.ts): through the file host's walk (files/fd-paths-win32.ts), as the host's
// node on Windows reaches a folder's file through nothing else; Node's own calls by path there follow every reparse
// point. A hook's execute bit cannot stop it there: Git for Windows runs a hook that starts with #! whatever its mode
// (git's run-command.c, is_executable), and Windows keeps no such bit (libuv's stat gives a file 0o666 or 0o444). So a
// hook that is not the user's own, unchanged, is refused outright, as the guest's rule refuses any write under
// .git/hooks: it is moved off its name, in its own folder, to one git runs no hook by, "<name> (refused by Surogate)",
// and nothing of it is deleted. A link the walk does not follow, at a protected name or a key place, keeps commands
// from running, as one whose end the walk cannot see.

import type { BigIntStats } from "node:fs";

import { fdPaths } from "../files/fd-paths.js";
import { identityOf } from "../files/identity.js";
import { folded, protectedInFolder } from "../files/protect.js";
import { basename, dirname, join } from "../session-path.js";
import { type HookScan, isGitHook, keyPlace } from "./hooks.js";

// The name a refused hook is given: one git never runs a hook by, as it runs each by its own name alone.
const REFUSED = " (refused by Surogate)";
const REFUSED_NAME = /\(refused by Surogate(?: [0-9]+)?\)$/;

// What tells whether a hook changed, by the walk's stats of its own name: a link there is never followed.
const described = (stats: BigIntStats): string => `${identityOf(stats).ino}:${stats.size}:${stats.mtimeMs}:${stats.ctimeMs}:${stats.mode}`;

/** The look through *folder*, by the walk: never rejects. */
export function scanWalked(folder: string): Promise<HookScan> {
  const scan: HookScan = { hooks: new Map(), unreadable: [], links: new Map(), dependencyLinks: new Map(), todos: [] };
  const walk = (dir: string, inGit: boolean): void => {
    let names: string[];
    try {
      names = fdPaths.readdir(dir);
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      // Gone while the walk went by.
      if (code !== "ENOENT" && code !== "ENOTDIR") scan.unreadable.push(dir);
      return;
    }
    const gitFolder = inGit && names.includes("HEAD");
    if (gitFolder || folded(basename(dir)) === ".git") scan.todos.push(join(dir, "rebase-merge", "git-rebase-todo"), join(dir, "sequencer", "todo"));
    for (const name of names) {
      const path = join(dir, name);
      // A name that is not valid UTF-8 reads back with U+FFFD, and no path reaches it.
      if (name.includes("�")) {
        scan.unreadable.push(path);
        continue;
      }
      let stats: BigIntStats;
      try {
        stats = fdPaths.lstat(path);
      } catch (error) {
        // Gone as the walk went by; anything else, a name the walk refuses among them, is not seen through.
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") scan.unreadable.push(path);
        continue;
      }
      const lower = name.toLowerCase();
      const hook = isGitHook(folder, path);
      if (stats.isSymbolicLink()) {
        if (hook) scan.hooks.set(path, described(stats));
        else if (protectedInFolder(folder, path) || keyPlace(folder, path)) scan.links.set(path, path);
        continue;
      }
      if (stats.isDirectory()) {
        const store = gitFolder && lower === "objects" && !fdPaths.exists(join(path, "HEAD"));
        if (lower !== "node_modules" && !store) walk(path, inGit || lower === ".git");
      } else if (hook && !REFUSED_NAME.test(name)) {
        scan.hooks.set(path, described(stats));
      }
    }
  };
  walk(folder, false);
  return Promise.resolve(scan);
}

/**
 * Each hook of *scan* not in *baseline* unchanged, refused: moved off its name, beside it. The keys it refused, and those
 * it could not move.
 */
export function refuseWalked(_folder: string, scan: HookScan, baseline: ReadonlyMap<string, string>): Promise<{ changed: string[]; stuck: string[] }> {
  const changed: string[] = [];
  const stuck: string[] = [];
  for (const [key, signature] of scan.hooks) {
    if (baseline.get(key) === signature || key.toLowerCase().endsWith(".sample")) continue;
    let moved = false;
    for (let count = 1; count < 100 && !moved; count += 1) {
      const to = join(dirname(key), `${basename(key)}${count === 1 ? REFUSED : REFUSED.replace(")", ` ${count})`)}`);
      if (fdPaths.exists(to)) continue;
      try {
        fdPaths.rename(key, to);
        moved = true;
      } catch {
        break;
      }
    }
    (moved ? changed : stuck).push(key);
  }
  return Promise.resolve({ changed, stuck });
}

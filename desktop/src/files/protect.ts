// What a write may not touch. The cloud's two lists of system and credential
// files, with their messages (surogates/tools/workspace_io/local.py), and the
// names in the folder where a change runs code outside the sandbox: srt's own
// write denies (sandbox-utils.js), which its mounts only cover for files that
// existed when a sandbox started, so the file API checks them on every write,
// and the rest of what git reads its config and hooks from (see protectedInFolder).

import { posix } from "node:path";

import { NUL_REFUSED, valueError } from "./answers.js";
import { fileOs } from "./os.js";
import { edgeOn, expandUser, inside, ledTo, realpath } from "./paths.js";

// A session's paths, POSIX on every OS (session-path.ts): files/ is on the agent disk, which holds no module of the
// app's but its own.
const { join } = posix;

const HOME_FILES = [
  ".ssh/authorized_keys", ".ssh/id_rsa", ".ssh/id_ed25519", ".ssh/config", ".bashrc", ".zshrc",
  ".profile", ".bash_profile", ".zprofile", ".netrc", ".pgpass", ".npmrc", ".pypirc",
];
const SYSTEM_FILES = ["/etc/sudoers", "/etc/passwd", "/etc/shadow"];
const HOME_FOLDERS = [".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".config/gh"];
const SYSTEM_FOLDERS = ["/etc/sudoers.d", "/etc/systemd"];
const SENSITIVE_PREFIXES = ["/etc/", "/boot/", "/usr/lib/systemd/"];
const SENSITIVE_PATHS = ["/var/run/docker.sock", "/run/docker.sock"];

// The characters a volume that compares names without case may take as ASCII letters, and the letters.
// Unicode's full case folding, as an ext4 casefold folder folds names, takes ſ (U+017F) to s, ß and ẞ to ss
// and the ligatures U+FB00 to U+FB06 to the letters they join; exFAT's and NTFS's upper-casing takes ſ to S
// and ı (U+0131) to I; Turkic folding takes İ (U+0130) to i. The Kelvin sign (U+212A) is K by canonical
// equivalence (folded), and k without case. APFS's folding is Apple's own and not documented: each is taken as
// one, and a Mac measures it (test/vm/darwin/measure/owners.ts). The guest's rule folds the same
// (vm/rule-match.h, sg_fold).
const FOLDS: ReadonlyMap<string, string> = new Map([
  ["\u017f", "s"], ["\u0131", "i"], ["\u0130", "i"], ["\u00df", "ss"], ["\u1e9e", "ss"],
  ["\ufb00", "ff"], ["\ufb01", "fi"], ["\ufb02", "fl"], ["\ufb03", "ffi"], ["\ufb04", "ffl"], ["\ufb05", "st"], ["\ufb06", "st"],
]);
const FOLDING = /[\u017f\u0131\u0130\u00df\u1e9e\ufb00-\ufb06]/g;
// The code points HFS+'s comparison of names ignores (Apple's Technical Note TN1150, "HFS Plus Volume Format", its
// case-insensitive string comparison): there a name that holds them is the name without them.
const IGNORED = /[\u200c-\u200f\u202a-\u202e\u206a-\u206f\ufeff]/g;

// *name* without the dots and spaces it ends with, which FAT's and exFAT's volumes drop from a name. A scan from the
// end, so a name of many dots takes time that grows with its length.
function untrailed(name: string): string {
  let end = name.length;
  while (end > 0 && (name[end - 1] === "." || name[end - 1] === " ")) end -= 1;
  return name.slice(0, end);
}

/**
 * One component, *name*, as the volume a folder lies on may take it: without the code points HFS+ ignores, then
 * without the dots and spaces it ends with; each character a volume may take as letters as those letters; then as
 * APFS compares names, by Unicode's canonical equivalence (its canonical decomposition, NFD: a name and its
 * decomposed spelling are one), then without case. Each name a volume takes as a protected one is that one, on every
 * volume and every OS. The guest's rule has no canonical equivalence, and needs none: the only character whose
 * decomposition is an ASCII letter is the Kelvin sign, which it takes as k, and a letter with a mark, in either
 * spelling, is no ASCII name, to APFS or to either fold.
 */
export const folded = (name: string): string => {
  // An ASCII name holds nothing a volume ignores or takes as letters, and is its own canonical decomposition.
  if (!NOT_ASCII.test(name)) return untrailed(name).toLowerCase();
  return untrailed(name.replace(IGNORED, "")).replace(FOLDING, (found) => FOLDS.get(found)!).normalize("NFD").toLowerCase();
};
const NOT_ASCII = /[^\x00-\x7f]/;

/** A folder-relative key's components, each folded. */
export const foldedParts = (key: string): string[] => key.split("/").map(folded);

// Matched at any depth, in any case, and as folded: stricter than srt's three levels.
export const PROTECTED_NAMES: ReadonlySet<string> = new Set([
  ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile",
  ".ripgreprc", ".mcp.json", ".vscode", ".idea",
]);
// Git's, which count in a dependency folder too.
export const GIT_NAMES: ReadonlySet<string> = new Set([".gitconfig", ".gitmodules"]);
// The folders package managers fill, at any depth. What they unpack is no shell's, editor's or
// agent's config for this folder, and packages ship .idea and .vscode folders (iconv-lite does):
// below one of these, only git's names and .git count.
export const DEPENDENCY_FOLDERS: ReadonlySet<string> = new Set(["node_modules", "site-packages", "dist-packages"]);
export const PROTECTED_PAIRS: ReadonlyArray<readonly [string, string]> = [
  [".claude", "commands"], [".claude", "agents"], [".git", "hooks"], [".git", "config"],
];
// The folders that hold protected names: renamed away and made again, each would hand a
// command its keys anew.
export const KEY_FOLDERS: ReadonlySet<string> = new Set(PROTECTED_PAIRS.map(([first]) => first));
// Inside a .git folder, at any depth (a submodule's git folder lies in its
// parent's .git/modules): the config files git reads, in the folder or through
// commondir, and the hooks. A config can name a program that git runs on its
// next status; so can a hook.
export const GIT_CONFIGS: ReadonlySet<string> = new Set(["config", "config.worktree", "commondir"]);

// check_write: a refusal for the model, or null. *path* is as the model wrote it: on a copy by the folder's path,
// *at*, and judged by the name it leads to there.
export function checkWrite(folder: string, home: string, path: string, at = folder): string | null {
  if (path.includes("\0")) throw valueError(NUL_REFUSED);
  const expanded = expandUser(path, home);
  const edge = edgeOn(folder, at);
  const real = (path: string) => realpath(path, undefined, edge).path;
  const resolved = ledTo(expanded.startsWith("/") ? expanded : `${at}/${expanded}`, edge).path;
  const files = new Set([...HOME_FILES.map((file) => real(join(home, file))), ...SYSTEM_FILES.map(real)]);
  const folders = [...HOME_FOLDERS.map((name) => join(home, name)), ...SYSTEM_FOLDERS].map((name) => `${real(name)}/`);
  if (files.has(resolved) || folders.some((prefix) => resolved.startsWith(prefix))) {
    return `Write denied: '${path}' is a protected system/credential file.`;
  }
  if (SENSITIVE_PREFIXES.some((prefix) => resolved.startsWith(prefix)) || SENSITIVE_PATHS.includes(resolved)) {
    return `Refusing to write to sensitive system path: ${path}\nUse the terminal tool with sudo if you need to modify system files.`;
  }
  return writeRefusal(at, resolved, path);
}

/**
 * Why a write of *key* in the folder is refused, in the words for *path*, as the request named it: one of its protected
 * names; or on Windows a name Explorer acts on when it shows the folder (os.ts). Null where neither refuses it.
 */
export function writeRefusal(folder: string, key: string, path = key): string | null {
  if (protectedInFolder(folder, key)) return inFolderRefusal(path);
  return key !== folder && inside(key, folder) && fileOs.explorer(key) ? explorerRefusal(path) : null;
}

// Whether a key in the folder names, or lies under, one of srt's protected names.
// A file named .git (a worktree's pointer) would send git to a config and hooks
// of the agent's choosing, so .git itself is protected too. In a .git folder,
// at any depth, so are the config files (config, config.worktree, commondir),
// anything under a hooks folder and anything under worktrees, where each
// linked worktree keeps a config of its own and a commondir that redirects git.
// The rest of a .git folder (HEAD, info, objects, refs, ...) is not. Below a
// DEPENDENCY_FOLDERS folder, only git's names count.
// Each part is looked at once, and what the parts before and after it hold is kept as the look goes on: a key
// is judged in time that grows with its length, however many parts it has. A guest's answer names keys too
// (vm/history.ts), as deep as a path may be, and many in one answer.
export function protectedInFolder(folder: string, key: string): boolean {
  if (key === folder || !inside(key, folder)) return false;
  const parts = foldedParts(key.slice(folder.length + 1));
  if (parts.at(-1) === ".git") return true;
  // Whether a dependency folder lies above the part looked at.
  let unpacked = false;
  // The last part that is a hooks folder or one of git's states, looked for once a .git part is met.
  let state: number | undefined;
  for (let i = 0; i < parts.length; i += 1) {
    const part = parts[i]!;
    const counts = !unpacked || part === ".git" || GIT_NAMES.has(part);
    const paired = KEY_FOLDERS.has(part) && PROTECTED_PAIRS.some(([first, second]) => part === first && parts[i + 1] === second);
    if (counts && (PROTECTED_NAMES.has(part) || paired)) return true;
    if (part === ".git") {
      state ??= parts.findLastIndex((below) => below === "hooks" || GIT_STATE.has(below));
      if (runsCode(parts, i + 1, state)) return true;
    }
    unpacked ||= DEPENDENCY_FOLDERS.has(part);
  }
  return false;
}

// What lies after a .git component. A paused rebase or cherry-pick keeps a todo
// whose exec lines git runs on --continue. Git makes and removes each as it works.
export const GIT_STATE: ReadonlySet<string> = new Set(["worktrees", "rebase-merge", "rebase-apply", "sequencer"]);

// Whether what lies after a .git component runs code: *parts* from *at* on, *state* being the last
// part of them all that is a hooks folder or one of git's states.
// In the git folder itself, hooks and configs count only directly under it:
// elsewhere (refs, logs) the names are a branch's or a tag's. A submodule's git
// folder lies under modules/<name>, and its name can hold slashes, so there the
// names count at any depth after the name's first component.
// ponytail: under modules/ the names are told apart by position only, so some
// paths are protected that run nothing: refs whose names hold hooks, config or a
// state name, and the whole git folder of a submodule whose name, after its first
// part, holds hooks or a state name or ends in a config name (tools/hooks,
// libs/config). Telling them apart needs .gitmodules.
function runsCode(parts: string[], at: number, state: number): boolean {
  const first = parts[at];
  // Below a submodule's name's first part: from there on.
  if (first === "modules" && parts.length > at + 2) return state >= at + 2 || GIT_CONFIGS.has(parts.at(-1) ?? "");
  return first !== undefined && (GIT_STATE.has(first) || first === "hooks" || (parts.length === at + 1 && GIT_CONFIGS.has(first)));
}

// A directory moved out of a DEPENDENCY_FOLDERS folder, the folder itself too, carries what was
// unpacked there, unjudged, to where a shell's, editor's or agent's name counts; an exchange moves
// both ways. The guest's rule refuses it (rule-match.h's sg_moved_out).
export function movesOutOfDependency(folder: string, from: string, to: string, exchange = false): boolean {
  const [out, back] = [dependencyFolder(folder, from) !== null, dependencyFolder(folder, to) !== null];
  return (out && !back) || (exchange && back && !out);
}

// The DEPENDENCY_FOLDERS folder *key* lies at or below in the folder, the outermost, or null.
export function dependencyFolder(folder: string, key: string): string | null {
  if (key === folder || !inside(key, folder)) return null;
  const parts = key.slice(folder.length + 1).split("/");
  const at = parts.findIndex((part) => DEPENDENCY_FOLDERS.has(folded(part)));
  return at < 0 ? null : join(folder, ...parts.slice(0, at + 1));
}

// A name Explorer acts on: a file the agent leaves in the folder can make Explorer fetch an icon or a library from a path
// the file names, and a UNC path sends the user's NTLM hash to that server (CVE-2025-24054, through .library-ms).
export function explorerRefusal(path: string): string {
  return `Write denied: '${path}' is a name Windows's Explorer acts on when it shows this folder.`;
}

export function inFolderRefusal(path: string): string {
  return `Write denied: '${path}' is protected in this folder: a change to it could run code outside the sandbox.`;
}

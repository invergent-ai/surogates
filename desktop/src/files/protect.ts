// What a write may not touch. The cloud's two lists of system and credential
// files, with their messages (surogates/tools/workspace_io/local.py), and the
// names in the folder where a change runs code outside the sandbox: srt's own
// write denies (sandbox-utils.js), which its mounts only cover for files that
// existed when a sandbox started, so the file API checks them on every write,
// and the rest of what git reads its config and hooks from (see protectedInFolder).

import { join } from "node:path";

import { valueError } from "./answers.js";
import { expandUser, inside, realpath } from "./paths.js";

const HOME_FILES = [
  ".ssh/authorized_keys", ".ssh/id_rsa", ".ssh/id_ed25519", ".ssh/config", ".bashrc", ".zshrc",
  ".profile", ".bash_profile", ".zprofile", ".netrc", ".pgpass", ".npmrc", ".pypirc",
];
const SYSTEM_FILES = ["/etc/sudoers", "/etc/passwd", "/etc/shadow"];
const HOME_FOLDERS = [".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".config/gh"];
const SYSTEM_FOLDERS = ["/etc/sudoers.d", "/etc/systemd"];
const SENSITIVE_PREFIXES = ["/etc/", "/boot/", "/usr/lib/systemd/"];
const SENSITIVE_PATHS = ["/var/run/docker.sock", "/run/docker.sock"];

// Matched at any depth and in any case: stricter than srt's three levels.
const PROTECTED_NAMES = new Set([
  ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile",
  ".ripgreprc", ".mcp.json", ".vscode", ".idea",
]);
const PROTECTED_PAIRS: ReadonlyArray<readonly [string, string]> = [
  [".claude", "commands"], [".claude", "agents"], [".git", "hooks"], [".git", "config"],
];
// Inside a .git folder, at any depth (a submodule's git folder lies in its
// parent's .git/modules): the config files git reads, in the folder or through
// commondir, and the hooks. A config can name a program that git runs on its
// next status; so can a hook.
const GIT_CONFIGS = new Set(["config", "config.worktree", "commondir"]);

const real = (path: string) => realpath(path).path;

// check_write: a refusal for the model, or null. *path* is as the model wrote it.
export function checkWrite(folder: string, home: string, path: string): string | null {
  if (path.includes("\0")) throw valueError("embedded null byte");
  const expanded = expandUser(path, home);
  const resolved = real(expanded.startsWith("/") ? expanded : `${folder}/${expanded}`);
  const files = new Set([...HOME_FILES.map((file) => real(join(home, file))), ...SYSTEM_FILES.map(real)]);
  const folders = [...HOME_FOLDERS.map((name) => join(home, name)), ...SYSTEM_FOLDERS].map((name) => `${real(name)}/`);
  if (files.has(resolved) || folders.some((prefix) => resolved.startsWith(prefix))) {
    return `Write denied: '${path}' is a protected system/credential file.`;
  }
  if (SENSITIVE_PREFIXES.some((prefix) => resolved.startsWith(prefix)) || SENSITIVE_PATHS.includes(resolved)) {
    return `Refusing to write to sensitive system path: ${path}\nUse the terminal tool with sudo if you need to modify system files.`;
  }
  return protectedInFolder(folder, resolved) ? inFolderRefusal(path) : null;
}

// Whether a key in the folder names, or lies under, one of srt's protected names.
// A file named .git (a worktree's pointer) would send git to a config and hooks
// of the agent's choosing, so .git itself is protected too. In a .git folder,
// at any depth, so are the config files (config, config.worktree, commondir),
// anything under a hooks folder and anything under worktrees, where each
// linked worktree keeps a config of its own and a commondir that redirects git.
// The rest of a .git folder (HEAD, info, objects, refs, ...) is not.
export function protectedInFolder(folder: string, key: string): boolean {
  if (key === folder || !inside(key, folder)) return false;
  const parts = key.slice(folder.length + 1).toLowerCase().split("/");
  return parts.at(-1) === ".git" || parts.some(
    (part, i) => PROTECTED_NAMES.has(part) || PROTECTED_PAIRS.some(([first, second]) => part === first && parts[i + 1] === second) ||
      (part === ".git" && runsCode(parts.slice(i + 1))),
  );
}

// What lies after a .git component.
function runsCode(rest: string[]): boolean {
  const last = rest.at(-1);
  return (last !== undefined && GIT_CONFIGS.has(last)) || rest.includes("hooks") || rest[0] === "worktrees";
}

export function inFolderRefusal(path: string): string {
  return `Write denied: '${path}' is protected in this folder: a change to it could run code outside the sandbox.`;
}

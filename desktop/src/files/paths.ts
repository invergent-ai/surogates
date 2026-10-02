// Paths as the cloud resolves them: CPython's non-strict os.path.realpath and
// os.path.expanduser, ported, then the folder containment of
// surogates/tools/utils/workspace_sandbox.py. In the file helper these run in the
// sandbox's view of the filesystem, whose mounts are the real boundary.

import { lstatSync, readFileSync, readlinkSync, statSync } from "node:fs";
import { posix } from "node:path";

import { osError, sandboxError, valueError } from "./answers.js";

// os.path.split and os.path.join, for absolute POSIX paths.
function pySplit(path: string): [string, string] {
  const i = path.lastIndexOf("/") + 1;
  let head = path.slice(0, i);
  if (head && head !== "/".repeat(head.length)) head = head.replace(/\/+$/, "");
  return [head, path.slice(i)];
}

function pyJoin(path: string, name: string): string {
  if (name.startsWith("/")) return name;
  return !path || path.endsWith("/") ? path + name : `${path}/${name}`;
}

// posixpath._joinrealpath with strict=False: an error reading a name means "not
// a link", and a loop leaves the rest of the path unresolved.
function joinRealPath(path: string, rest: string, seen: Map<string, string | null>): [string, boolean] {
  if (rest.startsWith("/")) {
    rest = rest.slice(1);
    path = "/";
  }
  while (rest) {
    const slash = rest.indexOf("/");
    const name = slash < 0 ? rest : rest.slice(0, slash);
    rest = slash < 0 ? "" : rest.slice(slash + 1);
    if (!name || name === ".") continue;
    if (name === "..") {
      if (path) {
        const [head, tail] = pySplit(path);
        path = tail === ".." ? pyJoin(pyJoin(head, ".."), "..") : head;
      } else {
        path = "..";
      }
      continue;
    }
    const next = pyJoin(path, name);
    let link = false;
    try {
      link = lstatSync(next).isSymbolicLink();
    } catch {
      link = false;
    }
    if (!link) {
      path = next;
      continue;
    }
    if (seen.has(next)) {
      const known = seen.get(next);
      if (known !== null && known !== undefined) {
        path = known;
        continue;
      }
      return [pyJoin(next, rest), false];
    }
    seen.set(next, null);
    let resolved: boolean;
    [path, resolved] = joinRealPath(path, readlinkSync(next, "utf8"), seen);
    if (!resolved) return [pyJoin(path, rest), false];
    seen.set(next, path);
  }
  return [path, true];
}

// os.path.realpath of an absolute path, and whether it ran into a symlink loop.
// realpath ends with abspath(), which tidies the part a loop left unresolved.
export function realpath(path: string): { path: string; loop: boolean } {
  const [resolved, ok] = joinRealPath("", path, new Map());
  return { path: ok ? resolved || "/" : posix.normalize(resolved).replace(/(.)\/+$/, "$1"), loop: !ok };
}

// Path.resolve() fails on a loop only when stat of the result runs into one.
function stillLoops(path: string): boolean {
  try {
    statSync(path);
    return false;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "ELOOP";
  }
}

// os.path.expanduser: a leading ~ is the home folder, ~name that user's home
// folder from the password file; anything else is left as it is.
export function expandUser(path: string, home: string): string {
  if (!path.startsWith("~")) return path;
  let end = path.indexOf("/", 1);
  if (end < 0) end = path.length;
  let userHome: string;
  if (end === 1) {
    userHome = home;
  } else {
    const found = homeOf(path.slice(1, end));
    if (found === null) return path;
    userHome = found;
  }
  return userHome.replace(/\/+$/, "") + path.slice(end) || "/";
}

function homeOf(user: string): string | null {
  let passwd: string;
  try {
    passwd = readFileSync("/etc/passwd", "utf8");
  } catch {
    return null;
  }
  for (const line of passwd.split("\n")) {
    const fields = line.split(":");
    if (fields[0] === user && fields.length >= 6) return fields[5] ?? null;
  }
  return null;
}

// Whether *path* is *folder* or lies inside it, by whole components.
export function inside(path: string, folder: string): boolean {
  return path === folder || path.startsWith(`${folder}/`);
}

// resolve: a path as the model wrote it, as a key in the folder.
export function resolveInFolder(folder: string, home: string, userPath: string): string {
  if (userPath.includes("\0")) throw valueError("embedded null byte");
  const expanded = expandUser(userPath, home);
  const { path, loop } = realpath(expanded.startsWith("/") ? expanded : pyJoin(folder, expanded));
  if (loop && stillLoops(path)) throw osError("ELOOP", path);
  if (!inside(path, folder)) {
    throw sandboxError(
      `Path traversal blocked: '${userPath}' resolves to '${path}' which is outside the workspace '${folder}'.`,
    );
  }
  return path;
}

// A key the server sent: only resolve's output, a resolved path in the folder, is one.
export function keyInFolder(folder: string, key: string): string {
  if (key.includes("\0")) throw valueError("embedded null byte");
  const resolved = key.startsWith("/") ? realpath(key) : null;
  if (!resolved || resolved.loop || resolved.path !== key || !inside(key, folder)) {
    throw sandboxError(`Not a path in this folder: '${key}'`);
  }
  return key;
}

// Paths as the cloud resolves them: CPython's non-strict os.path.realpath and
// os.path.expanduser, ported, then the folder containment of
// surogates/tools/utils/workspace_sandbox.py. In the file helper these run in the
// sandbox's view of the filesystem, whose mounts are the real boundary.

import { lstatSync, readFileSync, readlinkSync, statSync } from "node:fs";
import { posix } from "node:path";

import { Failure, NUL_REFUSED, osError, sandboxError, valueError } from "./answers.js";

// A thread's copy of a folder stands for the folder (spec, Section 13, "The user's computer"): the helper works in the
// copy, *folder*, and every request and answer names its files by the path of the folder the copy is of, *at*, as the
// thread's commands see them in the guest. So on a copy a path is in the folder's words from the first look at it to
// the answer. A name under the folder's path is looked at where it lies in the copy, and no other name is looked at
// at all: nothing outside the copy is this helper's to read, the folder itself least of all.
export interface Edge {
  at: string;
  folder: string;
}

// The edge of a helper whose folder is named by another path, *at*; none for a folder named by its own.
export const edgeOf = (folder: string, at: string): Edge | undefined => (at === folder ? undefined : { at, folder });

// Where a name under the folder's path lies in the copy; null for any other, which the copy holds nothing for.
const lying = (path: string, { at, folder }: Edge): string | null => (inside(path, at) ? folder + path.slice(at.length) : null);

/**
 * *path*, as it lies on this computer, by the name the folder's path gives it: a path in the copy; or one of the
 * folders the copy lies in, which is as far above the folder's path as it is above the copy. Any other is none of
 * the copy's, and is left as it is.
 */
export function shown(path: string, { at, folder }: Edge): string {
  if (inside(path, folder)) return at + path.slice(folder.length);
  for (let real = posix.dirname(folder), named = posix.dirname(at); real !== "/"; real = posix.dirname(real), named = posix.dirname(named)) {
    if (path === real) return named;
  }
  return path;
}

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
// a link", and a loop leaves the rest of the path unresolved. On a copy (*edge*) the names are the folder's.
function joinRealPath(path: string, rest: string, seen: Map<string, string | null>, edge?: Edge): [string, boolean] {
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
    let next = pyJoin(path, name);
    // No request is taken by the copy's own path (ledTo), so a path only comes to it by a link's words: there it is the
    // folder's, and is never said.
    if (edge && next === edge.folder) next = edge.at;
    const lies = edge ? lying(next, edge) : next;
    let link = false;
    try {
      link = lies !== null && lstatSync(lies).isSymbolicLink();
    } catch {
      link = false;
    }
    if (!link || lies === null) {
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
    [path, resolved] = joinRealPath(path, readlinkSync(lies, "utf8"), seen, edge);
    if (!resolved) return [pyJoin(path, rest), false];
    seen.set(next, path);
  }
  return [path, true];
}

// os.path.realpath of an absolute path, and whether it ran into a symlink loop.
// realpath ends with abspath(), which tidies the part a loop left unresolved.
// *links* gets each link it went through, as its path was spelled when it got there.
export function realpath(path: string, links = new Map<string, string | null>(), edge?: Edge): { path: string; loop: boolean } {
  const [resolved, ok] = joinRealPath("", path, links, edge);
  if (ok) return { path: resolved || "/", loop: false };
  // normpath keeps exactly two leading slashes, which posix.normalize folds into one.
  const lead = resolved.startsWith("//") && !resolved.startsWith("///") ? "/" : "";
  return { path: lead + posix.normalize(resolved).replace(/(.)\/+$/, "$1"), loop: true };
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

// Reads only /etc/passwd, so a user known only to NSS or LDAP is not found
// (Linux first).
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

// Whether no name on the way to *path*, nor its own, is a link. Nothing is followed to find out.
function linkless(path: string): boolean {
  for (let end = path.indexOf("/", 1); ; end = path.indexOf("/", end + 1)) {
    try {
      if (lstatSync(end < 0 ? path : path.slice(0, end)).isSymbolicLink()) return false;
    } catch {
      // Not there, or not this user's to look at: no link that leads anywhere.
    }
    if (end < 0) return true;
  }
}

/**
 * The edge of a helper on a copy, once the copy is seen to lie where its path says. A link at the copy's name, or on
 * the way to it, would lead every look and every write wherever it likes, the folder itself too: such a copy is no
 * folder of the app's own, and nothing is done in it. None for a folder named by its own path, which each of its
 * paths' own resolution covers.
 */
export function edgeOn(folder: string, at: string): Edge | undefined {
  const edge = edgeOf(folder, at);
  if (edge && !linkless(folder)) throw sandboxError("This thread's copy is not a folder of the app's own");
  return edge;
}

/**
 * Where *asked*, an absolute path, leads with every link followed, and whether it ran into a loop of links. On a
 * copy, in the folder's words. The copy's own path names nothing there: asked by it, however it is spelled, a path is
 * one outside the folder like any other, and nothing is looked at for it.
 */
export function ledTo(asked: string, edge: Edge | undefined): { path: string; loop: boolean } {
  if (!edge) return realpath(asked);
  const tidied = posix.normalize(asked).replace(/(.)\/+$/, "$1");
  if (inside(tidied, edge.folder)) return { path: tidied, loop: false };
  const { path, loop } = realpath(asked, undefined, edge);
  // A loop leaves the rest of a path as it was written, and tidied it can spell the copy's own: the folder's, then.
  return { path: inside(path, edge.folder) ? edge.at + path.slice(edge.folder.length) : path, loop };
}

// A refusal in the request's own words for a path. They name the folder as its caller does, so a helper on a copy
// answers them as they are (edge.ts).
function refused(message: string): Failure {
  const refusal = { type: "sandbox", message };
  return new Failure(refusal, () => refusal);
}

// resolve: a path as the model wrote it, as a key in the folder. On a copy the key is under the folder's path, *at*,
// and so is every path on the way to it: links and containment are judged in the copy, where the files lie.
export function resolveInFolder(folder: string, home: string, userPath: string, at = folder): string {
  if (userPath.includes("\0")) throw valueError(NUL_REFUSED);
  const expanded = expandUser(userPath, home).replace(/\/{2,}/g, "/");
  const edge = edgeOn(folder, at);
  const { path, loop } = ledTo(expanded.startsWith("/") ? expanded : pyJoin(at, expanded), edge);
  if (loop && (edge ? realpath(path, undefined, edge).loop : stillLoops(path))) throw osError("ELOOP", path);
  if (!inside(path, at)) {
    throw refused(
      `Path traversal blocked: '${userPath}' resolves to '${path}' which is outside the workspace '${at}'.`,
    );
  }
  return path;
}

// A key the server sent: only resolve's output, a resolved path in the folder, is one. Answered as the path it names
// on this computer: on a copy the key is under the folder's path, *at*, and its file is in the copy.
export function keyInFolder(folder: string, key: string, at = folder): string {
  if (key.includes("\0")) throw valueError(NUL_REFUSED);
  const edge = edgeOn(folder, at);
  const resolved = key.startsWith("/") ? realpath(key, undefined, edge) : null;
  if (!resolved || resolved.loop || resolved.path !== key || !inside(key, at)) {
    throw refused(`Not a path in this folder: '${key}'`);
  }
  return edge ? edge.folder + key.slice(at.length) : key;
}

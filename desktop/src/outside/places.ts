// Folders outside a chat's folder (spec, Part A): which real folder a known name means, which folder a chat may be
// granted to read, and which grant holds a path. Every path is the session's, the guest's (session-path.ts), as grants
// are kept. Electron-free: the main process passes app.getPath, so a Desktop OneDrive moved is found where it is.

import { closeSync } from "node:fs";

import { checkFolder, type FolderGuards } from "../binding/folder.js";
import type { FdPaths } from "../files/fd-paths.js";
import { inside, linkless } from "../files/paths.js";
import { guestPathOf, resolve } from "../session-path.js";

export const KNOWN_FOLDERS = ["Desktop", "Downloads", "Documents", "Pictures"] as const;

type KnownName = "desktop" | "downloads" | "documents" | "pictures";

/**
 * The guest path of the known folder *name*, in whatever case the agent writes it, as the OS places it; null for any
 * other name, or for one the OS keeps on no drive (a Desktop redirected to a network share), which no guest path names.
 */
export function knownFolder(name: string, getPath: (name: KnownName) => string): string | null {
  const known = KNOWN_FOLDERS.find((folder) => folder.toLowerCase() === name.toLowerCase());
  if (!known) return null;
  try {
    return guestPathOf(getPath(known.toLowerCase() as KnownName));
  } catch {
    return null;
  }
}

/**
 * The folder *folder* as it resolves, if a chat working in *chatFolder* may be granted to read it: refused wherever
 * checkFolder refuses a chat's folder, and for the chat's own folder or one in it, which the chat reads already and
 * its commands can write: a link they put on the way would lead the grant anywhere.
 */
export function grantable(folder: string, guards: FolderGuards, chatFolder: string): { ok: true; folder: string } | { ok: false; message: string } {
  const checked = checkFolder(folder, guards);
  if (!checked.ok) return { ok: false, message: checked.message };
  if (checked.path === chatFolder) return { ok: false, message: "That is this chat's folder already." };
  if (inside(checked.path, chatFolder)) return { ok: false, message: "That folder is in this chat's folder, which the chat reads already." };
  return { ok: true, folder: checked.path };
}

/**
 * The deepest of *grants* that holds *path*, when no name on the way to *path*, from the root down, nor its own, is a
 * link (a reparse point on Windows): the path then is where it is spelled, and stays in the grant. Otherwise null.
 * *path* must be absolute and plain: a '..' after a link would be the OS's to resolve through it. The grant's own
 * folder must be one *fd* really holds: on Windows a bound one (fd.bind), since a path in no bound folder, or above
 * one, is not looked at there, and would pass for one with nothing in it.
 */
export function withinGrant(grants: string[], path: string, fd: FdPaths): string | null {
  if (resolve(path) !== path) return null;
  const grant = grants.filter((root) => inside(path, root)).sort((a, b) => b.length - a.length)[0];
  if (grant === undefined) return null;
  try {
    closeSync(fd.hold(grant));
    // ponytail: every link refused, even one that leads back into the grant, as the Windows walk refuses each.
    return linkless(path, fd) ? grant : null;
  } catch {
    return null; // not there, not bound, or a name the file host refuses
  }
}

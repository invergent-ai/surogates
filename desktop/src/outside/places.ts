// Folders outside a chat's folder (spec, Part A): which real folder a known name means, which folder a chat may be
// granted to read, and which grant holds a path. Every path is the session's, the guest's (session-path.ts), as grants
// are kept. Electron-free: the main process passes app.getPath, so a Desktop OneDrive moved is found where it is.

import { checkFolder, type FolderGuards } from "../binding/folder.js";
import type { FdPaths } from "../files/fd-paths.js";
import { inside } from "../files/paths.js";
import { guestPathOf, resolve } from "../session-path.js";

export const KNOWN_FOLDERS = ["Desktop", "Downloads", "Documents", "Pictures"] as const;

type KnownName = "desktop" | "downloads" | "documents" | "pictures";

/** The guest path of the known folder *name*, in whatever case the agent writes it, as the OS places it; null for any other name. */
export function knownFolder(name: string, getPath: (name: KnownName) => string): string | null {
  const known = KNOWN_FOLDERS.find((folder) => folder.toLowerCase() === name.toLowerCase());
  return known ? guestPathOf(getPath(known.toLowerCase() as KnownName)) : null;
}

/**
 * The folder *folder* as it resolves, if a chat working in *chatFolder* may be granted to read it: refused wherever
 * checkFolder refuses a chat's folder, and for the chat's own folder, which it reads already.
 */
export function grantable(folder: string, guards: FolderGuards, chatFolder: string): { ok: true; folder: string } | { ok: false; message: string } {
  const checked = checkFolder(folder, guards);
  if (!checked.ok) return { ok: false, message: checked.message };
  if (checked.path === chatFolder) return { ok: false, message: "That is this chat's folder already." };
  return { ok: true, folder: checked.path };
}

/**
 * The deepest of *grants* that holds *path*, when no name from that grant's own down to *path*'s is a link (a reparse
 * point on Windows): the path then is where it is spelled, and stays in the grant. Otherwise null. *path* must be
 * absolute and plain: a '..' after a link would be the OS's to resolve through it. A name below the grant's that is not there
 * yet ends the walk, as nothing past it can lead anywhere.
 */
export function withinGrant(grants: string[], path: string, fd: FdPaths): string | null {
  if (resolve(path) !== path) return null;
  // ponytail: every link refused, even one that leads back into the grant, as the Windows walk refuses each.
  const grant = grants.filter((root) => inside(path, root)).sort((a, b) => b.length - a.length)[0];
  if (grant === undefined) return null;
  const names = path.slice(grant.length).split("/").filter((name) => name !== "");
  for (let i = 0, at = grant; ; at = `${at}/${names[i++]}`) {
    try {
      if (fd.lstat(at).isSymbolicLink()) return null;
    } catch (error) {
      const code = (error as NodeJS.ErrnoException | null)?.code;
      // The grant's own folder gone (or, on Windows, never bound) holds nothing.
      return i > 0 && (code === "ENOENT" || code === "ENOTDIR") ? grant : null;
    }
    if (i === names.length) return grant;
  }
}

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A file handed to the user to save: a download the page starts, for which the browser, or
// Surogate Desktop, asks where to save it. The page learns nothing of where it went.

// The longest name a file is saved under, in bytes of UTF-8: what a file's name may be nearly everywhere.
const NAME_MOST = 255;
const bytes = (text: string): number => new TextEncoder().encode(text).length;

/**
 * The name a file at *path* is saved under: the file's own, as a name and no more. Its last part,
 * with no control character and neither kind of slash, cut to what a file's name may be with its
 * extension kept. A path that ends in no name is saved as "file".
 */
export function savedName(path: string): string {
  const name = (path.split("/").at(-1) ?? "").replaceAll("\\", "_").replace(/\p{Cc}/gu, "");
  if (name.replace(/[. ]/g, "") === "") return "file";
  const dot = name.lastIndexOf(".");
  const tail = dot > 0 && bytes(name.slice(dot + 1)) <= 16 ? name.slice(dot) : "";
  let kept = "";
  // By whole characters: a name is never cut inside one.
  for (const character of tail ? name.slice(0, dot) : name) {
    if (bytes(kept + character) > NAME_MOST - bytes(tail)) break;
    kept += character;
  }
  return kept + tail;
}

/**
 * Hand *data* to the user to save as *name*. It reads nothing outside itself, so a test can run
 * it in a page as it is.
 */
export function saveFile(data: Blob, name: string): void {
  const url = URL.createObjectURL(data);
  const link = Object.assign(document.createElement("a"), { href: url, download: name });
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

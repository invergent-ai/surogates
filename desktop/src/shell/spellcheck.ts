// What the app's pages are spell-checked with: en-US alone, by Chromium's en-US Hunspell dictionary,
// which the app ships (scripts/dictionary.sh), and never one Chromium downloads from Google's servers
// for the person. Chromium asks its dictionary server for a session's dictionary the first time the
// session spell-checks, unless the file is in its own Dictionaries folder: the app puts it there
// before any session starts, and Chromium loads it as it is. Its downloads are pointed at the app's
// own folder too, by a file:// address, which this Electron does not fetch (it fails the download,
// on this computer): a copy that is gone or that Chromium refuses is then no word sent anywhere, and
// those pages are not spell-checked. Every session the app makes takes the same, the shell's own
// pages' and each agent's web client's.

import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import type { App, Session } from "electron";

// The one language: no other is spell-checked, whatever the computer's locale names.
export const LANGUAGES = ["en-US"];
// The dictionary this Electron's spellchecker asks for en-US, by Chromium's own versioned name.
export const DICTIONARY = "en-US-10-1.bdic";

/**
 * Put the app's dictionary, DICTIONARY in the folder *shipped*, in Chromium's Dictionaries folder under
 * *userData*, before any session starts: written whole or not at all, and not again while it is the same bytes.
 */
export function placeDictionary(shipped: string, userData: string): void {
  const own = readFileSync(join(shipped, DICTIONARY));
  const folder = join(userData, "Dictionaries");
  const placed = join(folder, DICTIONARY);
  let there: Buffer | null = null;
  try {
    there = readFileSync(placed);
  } catch {
    // None yet.
  }
  if (there?.equals(own)) return;
  // At a first run this makes the app's data folders before Chromium does: each the user's alone, as Chromium makes them.
  mkdirSync(folder, { recursive: true, mode: 0o700 });
  const next = `${placed}.${process.pid}.tmp`;
  writeFileSync(next, own);
  renameSync(next, placed);
}

/** *session* spell-checks in en-US alone, and asks any dictionary it lacks of the app's own folder, *shipped*. */
export function spellOwn(session: Pick<Session, "setSpellCheckerLanguages" | "setSpellCheckerDictionaryDownloadURL">, shipped: string): void {
  session.setSpellCheckerDictionaryDownloadURL(`${pathToFileURL(shipped).href}/`);
  session.setSpellCheckerLanguages(LANGUAGES);
}

/**
 * The app's spelling, from *shipped*, the app's own dictionaries folder: placed where Chromium looks, and every
 * session *app* makes from now on given it as it is made. Called before the app is ready, as the default
 * session is made then; a dictionary that cannot be placed is told *onError*, and its sessions are given the
 * rest all the same.
 */
export function ownSpelling(app: Pick<App, "getPath" | "on">, shipped: string, onError: (error: unknown) => void): void {
  try {
    placeDictionary(shipped, app.getPath("userData"));
  } catch (error) {
    onError(error);
  }
  app.on("session-created", (made) => spellOwn(made, shipped));
}

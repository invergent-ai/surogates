// This Mac's name, as the agent lists it and Settings shows it: the Computer Name the person gave it
// in System Settings → General → Sharing, which surogate-helper reads (native/helper, Name.swift). Its
// host name, os.hostname(), is the one Bonjour answers to, with ".local" after it: it is said without
// that only where the helper has no name to give.

import { hostname } from "node:os";

import { helperSync } from "../mac-helper.js";

// How long a name read is said again before it is read anew: Settings asks at each of its changes.
const KEPT_MS = 30_000;

// A name that holds no control character, and something else.
const usable = (name: string): boolean => name.trim() !== "" && !/[\0-\x1f\x7f]/.test(name);

/**
 * The name *read* gives, its line's end taken off; where it gives none, fails, or holds a control
 * character, *host* without ".local"; and that as it is where it is no name to show either.
 */
export function nameOf(read: () => string, host: () => string = hostname): string {
  let named = "";
  try {
    named = read().replace(/\r?\n$/, "");
  } catch {
    // No helper's name: the host's.
  }
  if (usable(named)) return named;
  const own = host().replace(/\.local$/i, "");
  return usable(own) ? own : host();
}

let kept: { at: number; name: string } | null = null;

export function computerName(): string {
  if (kept === null || Date.now() - kept.at >= KEPT_MS) kept = { at: Date.now(), name: nameOf(() => helperSync(["name"])) };
  return kept.name;
}

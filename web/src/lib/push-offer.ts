// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The plain parts of browser push (no browser APIs, no imports), so they can
// be tested alone: when the offer may show again, the server key's bytes, and
// the title that flashes while a message waits.

const DISMISSED_KEY = "surogates.push.dismissedAt";
/** "Not now" is respected this long before the offer comes back. */
export const DISMISS_DAYS = 30;

type Store = Pick<Storage, "getItem" | "setItem">;

function storage(): Store | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function offerDismissed(now = Date.now(), store = storage()): boolean {
  try {
    const at = Number(store?.getItem(DISMISSED_KEY) ?? 0);
    return now - at < DISMISS_DAYS * 24 * 3600 * 1000;
  } catch {
    return false;
  }
}

export function dismissOffer(now = Date.now(), store = storage()): void {
  try {
    store?.setItem(DISMISSED_KEY, String(now));
  } catch {
    // Private mode: the offer simply comes back next visit.
  }
}

/** A VAPID public key (base64url) as the bytes ``pushManager.subscribe`` takes. */
export function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const padded = (base64url + "===".slice((base64url.length + 3) % 4))
    .replace(/-/g, "+")
    .replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

const FLASH = "● New message";

/** The tab's title while *unread* messages wait: the flash alternates with the page's own. */
export function attentionTitle(
  base: string,
  unread: number,
  on: boolean,
): string {
  return unread > 0 && on ? `(${unread}) ${FLASH}` : base;
}

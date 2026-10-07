// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The user's computers and Surogate Desktop sign-ins at this agent, as Settings → Devices shows
// them (desktop design, Section 8). Its value imports are a package and a file of web/src named
// with its extension, and node strips its type-only import, so a node test runs it.

import { formatDistance } from "date-fns";

import { errorDetailMessage } from "../api/_errors.ts";
import type { Device, SignIn } from "../api/devices";

const ago = (at: string, now: Date): string =>
  formatDistance(new Date(at), now, { addSuffix: true });

/** How a computer is now: revoked, online, last seen, or never connected. */
export function deviceState(device: Device, now: Date = new Date()): string {
  if (device.revoked_at) {
    return `Revoked ${ago(device.revoked_at, now)}`;
  }
  if (device.online) {
    return "Online";
  }
  if (device.last_seen_at) {
    return `Last seen ${ago(device.last_seen_at, now)}`;
  }
  return "Never connected";
}

/** When a computer was added, and, once it has been, reauthorized. */
export function deviceHistory(device: Device, now: Date = new Date()): string {
  const added = `Added ${ago(device.created_at, now)}`;
  return device.reauthorized_at
    ? `${added}, reauthorized ${ago(device.reauthorized_at, now)}`
    : added;
}

/**
 * The desktop sign-ins the agent lists, or "sign-in-again": the agent shows them only to a
 * sign-in from the last 10 minutes, so a stolen session cannot end them.
 */
export async function signInsFrom(
  response: Response,
): Promise<SignIn[] | "sign-in-again"> {
  const body = (await response.json().catch(() => null)) as
    | { detail?: unknown }
    | SignIn[]
    | null;
  if (response.ok && Array.isArray(body)) {
    return body;
  }
  const detail = Array.isArray(body) ? undefined : body?.detail;
  const code =
    typeof detail === "object" && detail !== null
      ? (detail as { code?: unknown }).code
      : undefined;
  if (code === "recent_sign_in_required") {
    return "sign-in-again";
  }
  throw new Error(
    errorDetailMessage(detail) ?? "Failed to list your desktop sign-ins",
  );
}

// How long every web client of the user's tells them that a computer was added.
export const ADDED_NOTICE_MS = 7 * 24 * 60 * 60 * 1000;

/**
 * The computers to tell the user of: each added or reauthorized in the last 7 days that still
 * works, and not dismissed in this browser. Each is keyed by its computer and that time, so a
 * later reauthorization tells again.
 */
export function addedNotices(
  devices: Device[],
  now: Date,
  dismissed: ReadonlySet<string>,
): Array<{ key: string; name: string }> {
  return devices.flatMap((device) => {
    const at = device.reauthorized_at ?? device.created_at;
    const key = `${device.id}@${at}`;
    const recent = now.getTime() - Date.parse(at) < ADDED_NOTICE_MS;
    return device.revoked_at === null && recent && !dismissed.has(key)
      ? [{ key, name: device.name }]
      : [];
  });
}

export const addedNotice = (computer: string, agent: string): string =>
  `${computer} can now work on folders of your computer through ${agent}`;

// Where this browser keeps the notices its user dismissed.
const DISMISSED = "surogate:computers-told";

type KeptStorage = Pick<Storage, "getItem" | "setItem">;

/** The notices dismissed in this browser; none where its storage cannot be read. */
export function dismissedNotices(storage: KeptStorage | null): Set<string> {
  try {
    const kept = JSON.parse(storage?.getItem(DISMISSED) ?? "[]") as unknown;
    return new Set(
      Array.isArray(kept)
        ? kept.filter((key): key is string => typeof key === "string")
        : [],
    );
  } catch {
    return new Set();
  }
}

/**
 * Dismiss *key* in this browser, keeping only the dismissals of notices still *told*, so the list
 * never grows past them; where nothing can be kept, the page alone forgets it.
 */
export function dismissNotice(
  storage: KeptStorage | null,
  key: string,
  told: readonly string[],
): void {
  try {
    const kept = [...dismissedNotices(storage)].filter(
      (other) => other !== key && told.includes(other),
    );
    storage?.setItem(DISMISSED, JSON.stringify([...kept, key]));
  } catch {
    // Not kept: it shows again at the next page.
  }
}

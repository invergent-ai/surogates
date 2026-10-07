// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The user's computers and Surogate Desktop sign-ins at this agent, as Settings → Devices shows
// them (desktop design, Section 8). Every import here is a package or a file of web/src named
// with its extension, so a node test runs it.

import { formatDistance } from "date-fns";

import { errorDetailMessage } from "../api/_errors.ts";
import type { Device, SignIn } from "../api/devices";

const ago = (at: string, now: Date): string => formatDistance(new Date(at), now, { addSuffix: true });

/** How a computer is now: revoked, online, last seen, or never connected. */
export function deviceState(device: Device, now: Date = new Date()): string {
  if (device.revoked_at) return `Revoked ${ago(device.revoked_at, now)}`;
  if (device.online) return "Online";
  if (device.last_seen_at) return `Last seen ${ago(device.last_seen_at, now)}`;
  return "Never connected";
}

/** When a computer was added, and, once it has been, reauthorized. */
export function deviceHistory(device: Device, now: Date = new Date()): string {
  const added = `Added ${ago(device.created_at, now)}`;
  return device.reauthorized_at ? `${added}, reauthorized ${ago(device.reauthorized_at, now)}` : added;
}

/**
 * The desktop sign-ins the agent lists, or "sign-in-again": the agent shows them only to a
 * sign-in from the last 10 minutes, so a stolen session cannot end them.
 */
export async function signInsFrom(response: Response): Promise<SignIn[] | "sign-in-again"> {
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | SignIn[] | null;
  if (response.ok && Array.isArray(body)) return body;
  const detail = Array.isArray(body) ? undefined : body?.detail;
  const code = typeof detail === "object" && detail !== null ? (detail as { code?: unknown }).code : undefined;
  if (code === "recent_sign_in_required") return "sign-in-again";
  throw new Error(errorDetailMessage(detail) ?? "Failed to list your desktop sign-ins");
}

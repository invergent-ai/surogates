// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// How long a question stays answerable.
//
// The deadline itself is the server's: it is the only place that knows
// how long the tool call parks waiting, and it sends it as `expiresAt`.
// What is left here is presentation — turning that instant into a
// countdown and a yes/no.
//
// Studio carries the same two helpers in features/work/inbox-expiry.ts
// for its own inbox but cannot import these: its SDK pin predates the
// module. If a third surface needs them, export them from the package
// index and delete that copy rather than making a third.

import { useEffect, useState } from "react";

const TICK_MS = 30_000;

export function formatExpiresIn(
  expiresAtIso: string,
  nowMs: number,
): string {
  const remainingMs = Date.parse(expiresAtIso) - nowMs;
  if (remainingMs <= 0) return "Expired";
  if (remainingMs < 60_000) return "Expires in under a minute";
  const minutes = Math.round(remainingMs / 60_000);
  return `Expires in ~${minutes} minute${minutes === 1 ? "" : "s"}`;
}

/**
 * How much of the answer window is left, recomputed as it drains.
 *
 * The window can close while the user is looking at the question, so
 * whether it is still answerable cannot be read once at render: the
 * tool gives up shortly after, and an answer submitted in between is
 * recorded with nothing left to receive it.
 *
 * An item with no deadline — anything that is not a question — never
 * expires.
 */
export function useAnswerWindow(expiresAtIso: string | null | undefined): {
  expired: boolean;
  label: string;
} {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);
  if (!expiresAtIso) {
    return { expired: false, label: "" };
  }
  return {
    expired: Date.parse(expiresAtIso) <= now,
    label: formatExpiresIn(expiresAtIso, now),
  };
}

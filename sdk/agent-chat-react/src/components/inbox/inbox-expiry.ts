// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// How long a question stays answerable.
//
// Studio carries the same rule in features/work/inbox-expiry.ts for its
// own inbox but cannot import this one: its SDK pin predates the module.
// If a third surface needs it, export it from the package index and
// delete that copy rather than making a third.

import { useEffect, useState } from "react";

// The ask_user_question tool parks the turn for this long waiting for an
// answer (mirrors ASK_USER_QUESTION_MAX_WAIT_SECONDS on the server); the
// wait starts when the inbox item is created.
export const ANSWER_WINDOW_MS = 30 * 60 * 1000;

const TICK_MS = 30_000;

export function formatExpiresIn(createdAtIso: string, nowMs: number): string {
  const remainingMs = Date.parse(createdAtIso) + ANSWER_WINDOW_MS - nowMs;
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
 * sweeper expires the item shortly after, and an answer submitted in
 * between is recorded with no tool call left to receive it.
 */
export function useAnswerWindow(createdAtIso: string): {
  expired: boolean;
  label: string;
} {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);
  return {
    expired: Date.parse(createdAtIso) + ANSWER_WINDOW_MS <= now,
    label: formatExpiresIn(createdAtIso, now),
  };
}

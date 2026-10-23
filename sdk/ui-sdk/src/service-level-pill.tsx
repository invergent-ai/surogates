// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Where a piece of work stands against its service level, as a round pill.
// The state it is given decides the words and the tone, so the pill never
// second-guesses whoever measured it; the deadline and the clock only say how
// long is left, how long it has been past, and whether it falls due today.
import { type Tone, TonePill } from "./chip";

export type ServiceLevelState = "ok" | "past-goal" | "past-deadline";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * A span of time in its largest whole unit, rounded down: `5 min`, `3 h`,
 * `2 d`. Never less than `1 min`.
 */
export function formatDuration(ms: number): string {
  if (ms < HOUR) return `${Math.max(1, Math.floor(ms / MINUTE))} min`;
  if (ms < DAY) return `${Math.floor(ms / HOUR)} h`;
  return `${Math.floor(ms / DAY)} d`;
}

function toDate(value: string | Date | null | undefined): Date | null {
  if (value == null) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function sameLocalDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function standing(
  state: ServiceLevelState,
  deadline: Date | null,
  now: Date,
): { words: string; tone: Tone } {
  if (state === "past-deadline") {
    const past = deadline ? now.getTime() - deadline.getTime() : 0;
    return {
      words: past > 0 ? `Past deadline · ${formatDuration(past)}` : "Past deadline",
      tone: "bad",
    };
  }
  if (state === "past-goal") return { words: "Past goal", tone: "warn" };
  if (deadline && sameLocalDay(deadline, now)) {
    return { words: "Due today", tone: "warn" };
  }
  if (deadline && deadline.getTime() > now.getTime()) {
    return {
      words: `${formatDuration(deadline.getTime() - now.getTime())} left`,
      tone: "muted",
    };
  }
  return { words: "On time", tone: "muted" };
}

export function ServiceLevelPill({
  state,
  deadlineAt,
  now,
  paused = false,
  pausedLabel = "Paused",
  className,
}: {
  state: ServiceLevelState;
  /** An ISO timestamp or a `Date`. */
  deadlineAt?: string | Date | null;
  /** The clock to read against; the time of render when left out. */
  now?: Date;
  /** The service-level clock is stopped; the pill says so instead. */
  paused?: boolean;
  /** What a paused pill reads. */
  pausedLabel?: string;
  className?: string;
}) {
  const { words, tone } = paused
    ? { words: pausedLabel, tone: "info" as const }
    : standing(state, toDate(deadlineAt), now ?? new Date());
  return (
    <TonePill tone={tone} className={className}>
      {words}
    </TonePill>
  );
}

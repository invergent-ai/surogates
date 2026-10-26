// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A period picked from a short list of the last days: a muted label beside a
// small select, named by that label. A period that is not one of the choices
// (one an address asked for) still shows, in its numeric place in the list.
"use client";

import { useId } from "react";

import { cn } from "./lib/utils";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./select";

/** The periods offered unless the host gives its own, in days. */
export const PERIOD_CHOICES: readonly number[] = [7, 30, 90];

/** A period's words: the last days it covers. */
export function periodWords(days: number): string {
  return days === 1 ? "Last day" : `Last ${days} days`;
}

export function PeriodSelect({
  value,
  onChange,
  choices = PERIOD_CHOICES,
  label = "Period",
  className,
}: {
  /** The period shown, in days. */
  value: number;
  onChange: (days: number) => void;
  choices?: readonly number[];
  label?: string;
  className?: string;
}) {
  const labelId = useId();
  // Each period once, in numeric order, the shown one among them.
  const days = [...new Set([...choices, value])].sort((a, b) => a - b);

  return (
    <div className={cn("flex items-center gap-2", className)}>
      <span id={labelId} className="text-[13px] text-muted-foreground">
        {label}
      </span>
      <Select
        value={String(value)}
        onValueChange={(picked) => onChange(Number(picked))}
      >
        <SelectTrigger size="sm" aria-labelledby={labelId}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {days.map((choice) => (
            <SelectItem key={choice} value={String(choice)}>
              {periodWords(choice)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Figures side by side: a list the grid names, one stat tile per item, two
// to a row on a phone and as many as fit above it, and an optional caption
// under the list.

import type { ReactNode } from "react";

import { StatCard } from "./stat-card";

export function FigureGrid({
  label,
  caption,
  className,
  children,
}: {
  /** Names the list for a screen reader. */
  label: string;
  /** A note under the figures, such as what period they cover. */
  caption?: ReactNode;
  className?: string;
  /** The grid's `Figure`s. */
  children: ReactNode;
}) {
  return (
    <div className={className}>
      <ul
        aria-label={label}
        className="grid grid-cols-2 gap-2 sm:grid-cols-[repeat(auto-fit,minmax(8.5rem,1fr))]"
      >
        {children}
      </ul>
      {caption && (
        <p className="mt-2 text-[12px] text-muted-foreground">{caption}</p>
      )}
    </div>
  );
}

export function Figure({
  label,
  value,
  hint,
  detail,
  info,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  detail?: ReactNode;
  /** Words in a tooltip on the label. */
  info?: ReactNode;
}) {
  return (
    <li>
      <StatCard
        label={label}
        value={value}
        hint={hint}
        detail={detail}
        info={info}
        className="h-full rounded-xl border border-line bg-surface p-3.5"
      />
    </li>
  );
}

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { cn } from "./lib/utils";

interface ProgressBarProps {
  value: number;
  max?: number;
  color?: string;
  animated?: boolean;
  className?: string;
}

export function ProgressBar({
  value,
  max = 100,
  color = "var(--primary)",
  animated = false,
  className,
}: ProgressBarProps) {
  const pct = Math.min((value / max) * 100, 100);

  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={Math.min(value, max)}
      data-slot="progress-bar"
      className={cn("h-1 bg-line rounded-sm overflow-hidden", className)}
    >
      <div
        className="h-full rounded-sm transition-[width] duration-500 ease-out"
        style={{
          width: `${pct}%`,
          backgroundColor: color,
          animation: animated ? "progress-bar 1s ease" : undefined,
        }}
      />
    </div>
  );
}

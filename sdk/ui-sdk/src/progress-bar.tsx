// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//

interface ProgressBarProps {
  value: number;
  max?: number;
  color?: string;
  animated?: boolean;
}

export function ProgressBar({
  value,
  max = 100,
  color = "var(--primary)",
  animated = false,
}: ProgressBarProps) {
  const pct = Math.min((value / max) * 100, 100);

  return (
    <div className="h-1 bg-line rounded-sm overflow-hidden">
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

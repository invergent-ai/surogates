// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The one shape every running indicator uses: an inline activity orb
// beside a shimmering label. The orb is decorative (aria-hidden) — the
// adjacent label already announces the state to assistive tech.

import { ThinkingOrb, type OrbState } from "thinking-orbs";
import { cn } from "../../lib/utils";
import { Shimmer } from "./shimmer";

export interface OrbShimmerLabelProps {
  state: OrbState;
  label: string;
  /** Shimmer sweep duration in seconds. @default 3 */
  duration?: number;
  /** Shimmer spread; omitted uses the Shimmer default. */
  spread?: number;
  className?: string;
}

export function OrbShimmerLabel({
  state,
  label,
  duration = 3,
  spread = 3,
  className,
}: OrbShimmerLabelProps) {
  return (
    <span className={cn("flex items-center gap-2", className)}>
      <ThinkingOrb state={state} size={20} aria-hidden />
      <Shimmer duration={duration} spread={spread} className="text-sm">
        {label}
      </Shimmer>
    </span>
  );
}

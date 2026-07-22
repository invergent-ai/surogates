// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The platform-flavored border beam: always the Surogate amber `brand`
// palette, app-aware light/dark, and motion-safe. The rotate presets
// (`sm`/`md`/`line`) have no built-in `prefers-reduced-motion` handling
// upstream, so this wrapper deactivates them for reduced-motion users;
// the pulse presets already render a static frame on their own.

import { useEffect, useState, type ReactNode } from "react";
import { BorderBeam, type BorderBeamSize } from "./border-beam";

const ROTATE_SIZES: ReadonlySet<BorderBeamSize> = new Set(["sm", "md", "line"]);

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const handler = (e: MediaQueryListEvent) => setReduced(e.matches);
    mediaQuery.addEventListener("change", handler);
    return () => mediaQuery.removeEventListener("change", handler);
  }, []);

  return reduced;
}

export interface BrandBeamProps {
  children: ReactNode;
  /** Beam preset. Rotate: sm|md|line; pulse: pulse-inner|pulse-outside. */
  size?: BorderBeamSize;
  /** Whether the beam is showing; fades smoothly on change. */
  active?: boolean;
  /** Effect opacity 0–1; only affects the beam layers. */
  strength?: number;
  /** Animation cycle duration in seconds; preset default when omitted. */
  duration?: number;
  /** Explicit border radius in px; auto-detected from the child when omitted. */
  borderRadius?: number;
  className?: string;
}

/**
 * Wrap an element in the Surogate-branded border beam. Renders the
 * amber ramp in both themes and honors `prefers-reduced-motion`.
 */
export function BrandBeam({
  children,
  size = "md",
  active = true,
  strength,
  duration,
  borderRadius,
  className,
}: BrandBeamProps) {
  const reducedMotion = usePrefersReducedMotion();
  const effectiveActive =
    active && !(reducedMotion && ROTATE_SIZES.has(size));

  return (
    <BorderBeam
      size={size}
      colorVariant="brand"
      theme="auto"
      active={effectiveActive}
      strength={strength}
      duration={duration}
      borderRadius={borderRadius}
      className={className}
    >
      {children}
    </BorderBeam>
  );
}

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The platform-flavored border beam: always the Surogate amber `brand`
// palette, app-aware light/dark, and motion-safe. The rotate presets
// (`sm`/`md`/`line`) have no built-in `prefers-reduced-motion` handling
// upstream, so this wrapper deactivates them for reduced-motion users;
// the pulse presets already render a static frame on their own.

import { useSyncExternalStore, type ReactNode } from "react";
import { BorderBeam, type BorderBeamSize } from "./border-beam";

const ROTATE_SIZES: ReadonlySet<BorderBeamSize> = new Set(["sm", "md", "line"]);

// One shared matchMedia subscription for every BrandBeam instance —
// reduced motion is a global signal, so per-instance listeners would
// only multiply with wrapper count.
const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

function subscribeReducedMotion(onChange: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mediaQuery = window.matchMedia(REDUCED_MOTION_QUERY);
  mediaQuery.addEventListener("change", onChange);
  return () => mediaQuery.removeEventListener("change", onChange);
}

function readReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(REDUCED_MOTION_QUERY).matches;
}

function readReducedMotionServer(): boolean {
  return false;
}

export interface BrandBeamProps {
  children: ReactNode;
  /** Beam preset. Rotate: sm|md|line; pulse: pulse-inner|pulse-outside. */
  size?: BorderBeamSize;
  /** Whether the beam is showing; fades smoothly on change. */
  active?: boolean;
  /** Effect opacity 0–1; only affects the beam layers. */
  strength?: number;
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
  borderRadius,
  className,
}: BrandBeamProps) {
  const reducedMotion = useSyncExternalStore(
    subscribeReducedMotion,
    readReducedMotion,
    readReducedMotionServer,
  );
  const effectiveActive =
    active && !(reducedMotion && ROTATE_SIZES.has(size));

  return (
    <BorderBeam
      size={size}
      colorVariant="brand"
      theme="auto"
      active={effectiveActive}
      strength={strength}
      borderRadius={borderRadius}
      className={className}
    >
      {children}
    </BorderBeam>
  );
}

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
import { useEffect, useState } from "react";

/**
 * Keep an element mounted long enough for its exit animation to play.
 *
 * Returns `mounted` (whether to render) and `transitionState` ("entering",
 * "entered", or "exiting") which callers expose as a `data-state` attribute
 * so Tailwind variants can drive both enter and exit animations.
 *
 * - When `visible` flips true: mount immediately. State starts at
 *   "entering" for one frame so the CSS transition fires, then settles to
 *   "entered".
 * - When `visible` flips false: state becomes "exiting", element stays
 *   mounted for `durationMs`, then unmounts.
 */
export function useDelayedUnmount(
  visible: boolean,
  durationMs = 200,
): {
  mounted: boolean;
  transitionState: "entering" | "entered" | "exiting";
} {
  const [mounted, setMounted] = useState(visible);
  const [transitionState, setTransitionState] = useState<
    "entering" | "entered" | "exiting"
  >(visible ? "entered" : "exiting");

  useEffect(() => {
    if (visible) {
      setMounted(true);
      setTransitionState("entering");
      // Two-frame delay so the entering styles get committed before we
      // flip to the "entered" steady state and trigger the transition.
      const raf1 = requestAnimationFrame(() => {
        const raf2 = requestAnimationFrame(() => {
          setTransitionState("entered");
        });
        return () => cancelAnimationFrame(raf2);
      });
      return () => cancelAnimationFrame(raf1);
    }
    setTransitionState("exiting");
    const timer = window.setTimeout(() => {
      setMounted(false);
    }, durationMs);
    return () => window.clearTimeout(timer);
  }, [visible, durationMs]);

  return { mounted, transitionState };
}

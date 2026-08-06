// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
import { useEffect } from "react";

// Writes the visible viewport (accounting for the on-screen keyboard on iOS
// Safari / mobile Chrome) to `--viewport-h` and `--viewport-top` on the
// <html> element. The shell is a fixed box sized and positioned from these,
// so the composer stays pinned above the keyboard.
//
// The height alone is not enough: focusing an input near the bottom also
// makes the browser scroll the *layout* viewport up under the visual one, so
// a correctly-sized shell that still starts at the top of the layout viewport
// ends up shifted up with blank page below it — which is the "everything
// jumps up and the input is somewhere above the keyboard" bug.
// `visualViewport.offsetTop` is that scroll distance.
//
// Idempotent — safe to mount multiple times.
export function useVisualViewport() {
  useEffect(() => {
    const root = document.documentElement;
    const vv = window.visualViewport;

    function update() {
      // A pinch-zoom shrinks `height` too, and the shell is sized from it —
      // so only the unzoomed measurement means "the keyboard covers this".
      const zoomed = vv != null && Math.abs(vv.scale - 1) > 0.01;
      const h = zoomed || !vv ? window.innerHeight : vv.height;
      const top = zoomed || !vv ? 0 : Math.max(0, vv.offsetTop);
      root.style.setProperty("--viewport-h", `${h}px`);
      root.style.setProperty("--viewport-top", `${top}px`);
    }

    update();
    if (!vv) return;
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
    };
  }, []);
}

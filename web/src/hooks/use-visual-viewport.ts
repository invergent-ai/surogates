// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
import { useEffect } from "react";

// Writes the visible viewport height (accounting for the on-screen keyboard
// on iOS Safari / mobile Chrome) to a CSS custom property `--viewport-h` on
// the <html> element. The chat composer reads it to stay pinned above the
// keyboard. Idempotent — safe to mount multiple times.
export function useVisualViewport() {
  useEffect(() => {
    const root = document.documentElement;
    const vv = window.visualViewport;

    function update() {
      const h = vv?.height ?? window.innerHeight;
      root.style.setProperty("--viewport-h", `${h}px`);
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

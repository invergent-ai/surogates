import { useSyncExternalStore } from "react";

/**
 * The `md` breakpoint, matching the Tailwind default the rest of the SDK's
 * responsive classes use. Anything that needs to *render* differently on a
 * phone (rather than just restyle) reads this; a media query alone cannot
 * swap a popover for a sheet, because they are different components.
 */
const MOBILE_QUERY = "(max-width: 767px)";

// Deliberately not caching the MediaQueryList across calls. getSnapshot does
// run per render, but matchMedia is ~1µs and a module-level cache would pin
// the first `window.matchMedia` this module ever saw — which makes the hook
// untestable by stubbing it, and the sheet branch is only reachable in tests
// that way. Not a trade worth making for a cost that does not register.
function subscribe(onChange: () => void): () => void {
  const mql = window.matchMedia(MOBILE_QUERY);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
}

/**
 * True while the viewport is narrower than `md`.
 *
 * Reads synchronously on the first render rather than filling in from an
 * effect: a value that says "desktop" for one frame is invisible for a class
 * name and wrong for anything that decides *which component to mount*.
 */
export function useIsMobile(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(MOBILE_QUERY).matches,
    // No viewport when server-rendered; assume desktop, as the SDK does.
    () => false,
  );
}

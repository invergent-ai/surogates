import { useSyncExternalStore } from "react";

/**
 * The `md` breakpoint, matching the Tailwind default the rest of the SDK's
 * responsive classes use. Anything that needs to *render* differently on a
 * phone (rather than just restyle) reads this; a media query alone cannot
 * swap a popover for a sheet, because they are different components.
 */
const MOBILE_QUERY = "(max-width: 767px)";

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

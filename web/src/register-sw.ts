// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Registers the installability worker (see public/sw.js — it caches
// nothing). Kept out of main.tsx so the conditions under which we skip
// registration are stated in one place.

export function registerServiceWorker(): void {
  // A worker registered against localhost outlives the dev server and is
  // scoped to the origin, not the project — so it would sit under every
  // other Vite app that ever uses that port. Installability is a
  // production concern anyway.
  if (!import.meta.env.PROD) {
    return;
  }

  if (!("serviceWorker" in navigator)) {
    return;
  }

  // A worker is only allowed on a secure origin, and `localhost` counts.
  // Dev over plain http on a LAN address does not, so registration would
  // throw there rather than no-op.
  if (!window.isSecureContext) {
    return;
  }

  // Registration competes with the first paint for bandwidth and the main
  // thread; the install prompt is not needed in that first second.
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {
      // Installability is a nicety — a browser that refuses the worker
      // still gets the whole app.
    });
  });
}

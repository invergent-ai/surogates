// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// This worker exists so the app is installable, and for nothing else.
//
// Android Chrome still gates "Install app" on a registered service worker
// that handles fetch. It deliberately does NOT cache: Studio is an
// authenticated app against a live agent runtime, so a cached bundle or a
// cached API response is a stale-build bug and a data-leak-across-accounts
// bug waiting to happen, and it buys nothing — the app is useless offline.
//
// The fetch handler therefore observes and forwards. Not calling
// respondWith() lets the browser do exactly what it would have done
// without a worker, while still satisfying the installability check.
//
// If real offline support is ever wanted, that is a precache of the built
// assets keyed by build hash plus a network-first rule for /api — a
// different file, written deliberately, not an extension of this one.

self.addEventListener("install", () => {
  // Take over immediately rather than waiting for every tab to close, so a
  // deploy never leaves two worker versions alive at once.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // No cache sweep here. This worker has never written one, and
  // `caches.keys()` is origin-wide — clearing it would throw away storage
  // belonging to anything else served from this host.
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", () => {
  // Intentionally empty — see the note above.
});

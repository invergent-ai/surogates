// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// This worker exists so the app is installable and can show push
// notifications, and for nothing else.
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

// Push: the agent wrote to this person while they were away (a journey
// step, an alert, a reply). The payload is the server's, encrypted for this
// browser: {title, body, url, session_id}. One notification per
// conversation, replaced by the newest, and the app icon's badge counts them.
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    (async () => {
      await self.registration.showNotification(data.title || "New message", {
        body: data.body || "",
        icon: "/icons/icon-192.png",
        badge: "/icons/icon-192.png",
        tag: data.session_id || "message",
        renotify: true,
        data: { url: data.url || "/" },
      });
      const shown = await self.registration.getNotifications();
      if (self.navigator.setAppBadge) {
        await self.navigator.setAppBadge(shown.length).catch(() => {});
      }
    })(),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/", self.location.origin)
    .href;
  event.waitUntil(
    (async () => {
      const open = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      for (const client of open) {
        if (client.url.startsWith(self.location.origin) && "focus" in client) {
          await client.focus();
          if ("navigate" in client) {
            await client.navigate(url).catch(() => {});
          }
          return;
        }
      }
      await self.clients.openWindow(url);
    })(),
  );
});

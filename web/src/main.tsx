// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./index.css";
import { App } from "./app/app";
import {
  applyTranscript,
  followTranscript,
  takesDesktopLook,
  transcriptOf,
} from "./lib/appearance";
import { getDesktop } from "./lib/desktop-bridge";
import { registerServiceWorker } from "./register-sw";

const globalCrypto = globalThis.crypto as Crypto | undefined;

if (globalCrypto && typeof globalCrypto.randomUUID !== "function") {
  // Some envs ship `crypto` but no `randomUUID()` (or a non-function stub).
  // Provide a best-effort v4 UUID using `getRandomValues` when available.
  const cryptoRef = globalCrypto;

  function getRandomByte(): number {
    if (typeof cryptoRef.getRandomValues === "function") {
      return cryptoRef.getRandomValues(new Uint8Array(1))[0];
    }
    return Math.floor(Math.random() * 256);
  }

  cryptoRef.randomUUID = (() =>
    "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) =>
      (+c ^ (getRandomByte() & (15 >> (+c / 4)))).toString(16),
    )) as Crypto["randomUUID"];
}

// In Surogate Desktop the transcript is as the desktop's Settings shape it: from now on, through
// the bridge, or in the pane, which has none, as its address says. Its theme is on the root from
// the first frame (index.html).
const desktop = getDesktop();
if (desktop) {
  followTranscript(desktop, document.documentElement);
} else if (takesDesktopLook(desktop, window.location.pathname)) {
  applyTranscript(
    document.documentElement,
    transcriptOf(window.location.search),
  );
}

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element not found");
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// Chrome gates its install prompt on a registered worker that handles fetch.
// This one caches nothing — see public/sw.js.
registerServiceWorker();

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// How Surogate Desktop's Settings shape the web client's transcript (desktop design, Progress:
// "Appearance is a setting"): its text size, its width and its motion, set on the document's root
// as data-text-size, data-transcript-width and data-motion, which index.css and the SDK read. The
// theme needs nothing here: the desktop's choice drives prefers-color-scheme in every page it
// shows, and the web client follows that in the desktop (app/provider.tsx).

import type {
  DesktopAppearance,
  DesktopBridge,
} from "./desktop-bridge-contract";

export type TranscriptSettings = Pick<
  DesktopAppearance,
  "textSize" | "transcriptWidth" | "motion"
>;

// What the desktop's Settings offer; Medium and System are the web client's own look.
const CHOICES: Record<keyof TranscriptSettings, readonly string[]> = {
  textSize: ["small", "medium", "large"],
  transcriptWidth: ["narrow", "medium", "wide"],
  motion: ["system", "reduced"],
};

/** Set *settings* on *root*: each value the desktop offers, and none for anything else. */
export function applyTranscript(
  root: { dataset: Record<string, string | undefined> },
  settings: Partial<Record<keyof TranscriptSettings, unknown>>,
): void {
  for (const [key, allowed] of Object.entries(CHOICES)) {
    const value = settings[key as keyof TranscriptSettings];
    if (typeof value === "string" && allowed.includes(value)) {
      root.dataset[key] = value;
    } else {
      delete root.dataset[key];
    }
  }
}

/**
 * Follow the desktop's transcript settings on *root*: as they are now, then each change. A change
 * heard before the first read answers is newer than that answer, which is then dropped. Returns
 * what stops it.
 */
export function followTranscript(
  desktop: Pick<DesktopBridge, "getAppearance" | "onAppearanceChanged">,
  root: { dataset: Record<string, string | undefined> },
): () => void {
  let heard = false;
  const stop = desktop.onAppearanceChanged((appearance) => {
    heard = true;
    applyTranscript(root, appearance);
  });
  desktop.getAppearance().then(
    (appearance) => {
      if (!heard) {
        applyTranscript(root, appearance);
      }
    },
    (error: unknown) =>
      console.warn(
        "Surogate Desktop could not say how its transcript looks",
        error,
      ),
  );
  return stop;
}

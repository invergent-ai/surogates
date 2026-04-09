// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useSyncExternalStore, useCallback } from "react";

const listeners = new Set<() => void>();

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

function getSnapshot() {
  return document.documentElement.classList.contains("dark");
}

function notify() {
  for (const cb of listeners) cb();
}

// Initialise theme on module load
const stored = localStorage.getItem("theme");
const prefersDark = window.matchMedia(
  "(prefers-color-scheme: dark)",
).matches;
const initialDark = stored === "dark" || (!stored && prefersDark);
document.documentElement.classList.toggle("dark", initialDark);

export function useTheme() {
  const isDark = useSyncExternalStore(subscribe, getSnapshot);

  const toggle = useCallback(() => {
    const next = !document.documentElement.classList.contains("dark");
    document.documentElement.classList.toggle("dark", next);
    localStorage.setItem("theme", next ? "dark" : "light");
    notify();
  }, []);

  return { isDark, toggle };
}

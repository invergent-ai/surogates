// Quick entry on macOS: Option+Space, from anywhere. Linux's Ctrl+Alt+Space is macOS's own "Select next
// source in Input menu". Electron's globalShortcut takes the keys as a hot key of the app's, from every
// other app: while Surogate runs, Option+Space types no non-breaking space anywhere. Where another app
// holds them already, Settings says so, and the menu bar's icon opens quick entry.

import type { App, BrowserWindow } from "electron";

// In Electron's accelerator's words: Alt is the Option key.
const KEYS = "Alt+Space";

/** The keys that open quick entry: the same in every session of macOS's. */
export const quickEntryKeys = (_env: NodeJS.ProcessEnv): string | null => KEYS;

/** What Settings says while another app holds the keys. */
export const unheld = (_keys: string): string | null =>
  "Another app holds Option+Space, so it does not open quick entry. Open quick entry from Surogate's icon in the menu bar.";

/**
 * *window* shown with the keyboard, over the app that had it when the keys were pressed: the app made
 * the active one first (Electron's app.focus with steal, Apple's NSApplication activate). Unsure: macOS
 * 14 lets an app take the focus only as the system agrees to (Apple, NSApplication, "Cooperative app
 * activation"); that a hot key's press is reason enough is checked on a Mac.
 */
export function present(app: Pick<App, "focus">, window: BrowserWindow): void {
  app.focus({ steal: true });
  window.show();
  window.focus();
}

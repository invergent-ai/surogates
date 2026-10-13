// Quick entry on Windows: Ctrl+Alt+Space, from anywhere, as on Linux. Electron's globalShortcut takes the keys as a
// hot key of the app's (Microsoft, RegisterHotKey), which no other app may hold at once: where one does, Settings says
// so, and the notification area's icon opens quick entry. On a keyboard layout with AltGr, Windows reads AltGr as
// Ctrl+Alt (Microsoft, "Keyboard Input", AltGr), so AltGr+Space opens quick entry too. Unsure: whether a layout that
// types a character with AltGr+Space loses it to the hot key is checked on a PC with a German layout.

import type { App, BrowserWindow } from "electron";

const KEYS = "Ctrl+Alt+Space";

/** The keys that open quick entry: the same in every session of Windows's. */
export const quickEntryKeys = (_env: NodeJS.ProcessEnv): string | null => KEYS;

/** What Settings says while another app holds *keys*. */
export const unheld = (keys: string): string | null =>
  `Another app holds ${keys}, so it does not open quick entry. Open quick entry from Surogate's icon in the notification area.`;

/**
 * *window* shown, with the keyboard. Windows lets the process that received the last input event, as a hot key's
 * press is, bring its window to the foreground (Microsoft, SetForegroundWindow, "The process received the last input
 * event").
 */
export function present(_app: Pick<App, "focus">, window: BrowserWindow): void {
  window.show();
  window.focus();
}

// What of quick entry differs by this OS (quick-entry-linux.ts, quick-entry-darwin.ts): the keys it takes,
// what Settings says while another app holds them, and how its window takes the keyboard. Electron-free.

import type { App, BrowserWindow } from "electron";

import { forThisOs, notBuilt } from "../platform.js";
import * as darwin from "./quick-entry-darwin.js";
import * as linux from "./quick-entry-linux.js";

interface QuickEntry {
  quickEntryKeys(env: NodeJS.ProcessEnv): string | null;
  unheld(keys: string): string | null;
  present(app: Pick<App, "focus">, window: BrowserWindow): void;
}
export const QUICK_ENTRY: QuickEntry = forThisOs<QuickEntry>({ linux, darwin }) ?? {
  quickEntryKeys: () => notBuilt("Surogate Desktop's quick entry shortcut"),
  unheld: () => notBuilt("Surogate Desktop's quick entry shortcut"),
  present: () => notBuilt("Surogate Desktop's quick entry"),
};

/** The keys that open quick entry from anywhere, by this OS, or null where the app takes none. */
export const quickEntryKeys = (env: NodeJS.ProcessEnv): string | null => QUICK_ENTRY.quickEntryKeys(env);

/** What Settings says while another app holds *keys*, or null where this OS says it in the log alone. */
export const unheldWords = (keys: string): string | null => QUICK_ENTRY.unheld(keys);

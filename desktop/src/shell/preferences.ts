// How the app behaves, beside how it looks (appearance.ts): whether it keeps running once its
// window is closed, on unless the user turns it off (spec, Section 7), and whether its menu has
// Developer. Kept in the app-state root, in a file of its own: the appearance store writes its file whole.

import { readState, writeState } from "./state-file.js";

export interface Preferences {
  keepRunning: boolean;
  developer: boolean;
}

export const PREFERENCES: Readonly<Preferences> = { keepRunning: true, developer: false };

export class PreferencesStore {
  constructor(private readonly path: string) {}

  get(): Preferences {
    const saved = readState<Record<string, unknown>>(this.path, {});
    const preferences = { ...PREFERENCES };
    for (const key of Object.keys(PREFERENCES) as Array<keyof Preferences>) {
      if (typeof saved[key] === "boolean") preferences[key] = saved[key];
    }
    return preferences;
  }

  set(key: string, value: unknown): Preferences {
    if (!Object.hasOwn(PREFERENCES, key) || typeof value !== "boolean") throw new Error(`No preference ${key} = ${String(value)}`);
    const next = { ...this.get(), [key]: value };
    writeState(this.path, next);
    return next;
  }
}

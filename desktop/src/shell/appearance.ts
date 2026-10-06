// How the app looks (Progress, decisions of 2026-10-06): Light, Dark or Match system,
// the default, as Claude Desktop offers it, and the transcript's text size, width and
// motion, which the web client reads. Kept in the app-state root, and in effect
// before the first window paints.

import { readState, writeState } from "./state-file.js";

export const CHOICES = {
  theme: ["system", "light", "dark"],
  textSize: ["small", "medium", "large"],
  transcriptWidth: ["narrow", "medium", "wide"],
  motion: ["system", "reduced"],
} as const;

export type Appearance = { -readonly [K in keyof typeof CHOICES]: (typeof CHOICES)[K][number] };

export const DEFAULTS: Appearance = { theme: "system", textSize: "medium", transcriptWidth: "medium", motion: "system" };

const allowed = (key: string, value: unknown): key is keyof Appearance =>
  Object.hasOwn(CHOICES, key) && (CHOICES[key as keyof Appearance] as readonly unknown[]).includes(value);

export class AppearanceStore {
  constructor(private readonly path: string) {}

  get(): Appearance {
    const saved = readState<Record<string, unknown>>(this.path, {});
    const appearance = { ...DEFAULTS };
    for (const [key, value] of Object.entries(saved)) {
      if (allowed(key, value)) Object.assign(appearance, { [key]: value });
    }
    return appearance;
  }

  set(key: string, value: unknown): Appearance {
    if (!allowed(key, value)) throw new Error(`No appearance setting ${key} = ${String(value)}`);
    const next = { ...this.get(), [key]: value };
    writeState(this.path, next);
    return next;
  }
}

// What the shell paints outside its pages, as Claude Desktop does: the window's background,
// and the system's controls over the top right corner, on the Overview pane's colour.
export function chrome(dark: boolean): { background: string; overlay: { color: string; symbolColor: string; height: number } } {
  return dark
    ? { background: "#151515", overlay: { color: "#1a1a19", symbolColor: "#c2c0b6", height: 40 } }
    : { background: "#faf9f5", overlay: { color: "#f5f4ed", symbolColor: "#3d3d3a", height: 40 } };
}

// Electron's nativeTheme, as far as the shell uses it.
export interface ThemeSource {
  themeSource: Appearance["theme"];
  readonly shouldUseDarkColors: boolean;
  on(event: "updated", listener: () => void): unknown;
}

/** The theme in effect: the saved choice from the start; *paint* hears each change, the system's under Match system too. */
export class Theme {
  constructor(private readonly native: ThemeSource, private readonly store: AppearanceStore, paint: (dark: boolean) => void) {
    native.themeSource = store.get().theme;
    native.on("updated", () => paint(native.shouldUseDarkColors));
  }

  get dark(): boolean {
    return this.native.shouldUseDarkColors;
  }

  choose(theme: Appearance["theme"]): void {
    this.store.set("theme", theme);
    this.native.themeSource = theme;
  }
}

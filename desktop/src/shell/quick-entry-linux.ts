// Quick entry's shortcut on Linux: Claude Desktop's own, from anywhere on the display, where the X
// server grabs keys for an app. A Wayland session has its GlobalShortcuts portal instead, which
// Surogate does not ask: there the tray opens quick entry.

// Its shortcut, Claude Desktop's own on Linux.
const KEYS = "Ctrl+Alt+Space";

/**
 * A Wayland session, as Claude Desktop tells one: by its session type, or else by a Wayland display.
 * Electron's globalShortcut grabs keys from the X server, and no app's grab is global there.
 */
const waylandSession = (env: NodeJS.ProcessEnv): boolean => (env.XDG_SESSION_TYPE ? env.XDG_SESSION_TYPE === "wayland" : Boolean(env.WAYLAND_DISPLAY));

/** The keys that open quick entry in the session *env* describes, or null in one where the app takes none. */
export const quickEntryKeys = (env: NodeJS.ProcessEnv): string | null => (waylandSession(env) ? null : KEYS);

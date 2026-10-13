// What of the app's menus is Windows's own: Linux's menus, as Windows has no app menu and gives a window its own menu
// bar as Linux does (menus-linux.ts); and the notification area's icon, an .ico, which holds each size Windows draws
// it at (16, 20, 24 and 32 pixels, at 100 to 200 per cent; Microsoft, "Notifications and the Notification Area",
// NOTIFYICONDATA's hIcon, and "Icons"). Its colours follow Windows's own mode for its taskbar, which a person sets apart
// from the apps' (Settings → Personalization → Colors, "Choose your default Windows mode"): light on a dark taskbar,
// dark on a light one.

export { firstMenus, lastMenus } from "./menus-linux.js";

/** The notification area's icon, for a taskbar that is *systemDark* or not; the apps' theme and the desktop are Linux's. */
export function trayIcon(_dark: boolean, _desktop: string | undefined, systemDark: boolean | undefined): "tray-dark.ico" | "tray-light.ico" {
  return systemDark ? "tray-dark.ico" : "tray-light.ico";
}

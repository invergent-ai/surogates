// The desktop shell's main process (spec, Sections 1 and 7): one window, as Claude
// Desktop has, and the device link that stays up with that window closed. It never
// runs what a tool call asks for. Readiness is awaited with then(), never a top-level
// await: an ES module main that awaits app.whenReady() deadlocks.

import { join } from "node:path";

import { app, nativeTheme } from "electron";

import { AppearanceStore, Theme } from "./appearance.js";
import { letWindowClose, MainWindow } from "./main-window.js";
import { WindowStates } from "./window-state.js";

const PAGES = join(import.meta.dirname, "pages");

// Everything the app keeps lives under one root, Electron's own data too: the folder
// guards refuse it as a chat's folder, so no agent reaches a device token through one.
const dataHome = process.env.XDG_DATA_HOME?.startsWith("/") ? process.env.XDG_DATA_HOME : join(app.getPath("home"), ".local", "share");
const root = join(dataHome, "surogate");
// Before ready: the OS keyring names its item after the app.
app.setName("Surogate");
app.setPath("userData", join(root, "electron"));

const states = new WindowStates(join(root, "window-state.json"));
const appearance = new AppearanceStore(join(root, "settings.json"));
let main: MainWindow | null = null;

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => main?.show());
  // The device link stays up with the window closed (spec, Section 7).
  app.on("window-all-closed", () => {});
  app.on("before-quit", letWindowClose);
  void app.whenReady().then(() => {
    // Before the window: its first frame is in the chosen theme.
    const theme = new Theme(nativeTheme, appearance, (dark) => main?.paint(dark));
    main = new MainWindow({ states, page: join(PAGES, "shell.html"), dark: theme.dark });
  });
}

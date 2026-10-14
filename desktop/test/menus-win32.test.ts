// The app's menus on Windows, which has no app menu: Linux's, File first with Settings… and Quit, Help last with About.
// The notification area's icon is an .ico, light on a dark taskbar and dark on a light one, as Windows's own mode for
// its taskbar says, whatever the apps' mode is.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import type { MenuActions } from "../src/shell/menus.js";
import { importedOn } from "./platforms.js";

const actions = (): MenuActions => ({
  newChat: () => {}, settings: () => {}, quit: () => {}, reload: () => {}, zoom: () => {}, devTools: () => {}, documentation: () => {}, about: () => {},
});
const plain = (value: unknown) => JSON.parse(JSON.stringify(value)) as unknown;

// The images an .ico holds, by their width and height (Microsoft, "Icons", the ICONDIR and ICONDIRENTRY structures).
function sizesIn(file: string): string[] {
  const bytes = readFileSync(file);
  expect([bytes.readUInt16LE(0), bytes.readUInt16LE(2)], file).toEqual([0, 1]);
  return Array.from({ length: bytes.readUInt16LE(4) }, (_, n) => {
    const [width, height] = [bytes[6 + n * 16]!, bytes[7 + n * 16]!].map((size) => size || 256);
    return `${width}x${height}`;
  });
}

describe("the app's menus on Windows", () => {
  it("are Linux's", async () => {
    const [windows, linux] = [await importedOn("win32", () => import("../src/shell/menus.js")), await importedOn("linux", () => import("../src/shell/menus.js"))];
    for (const developer of [true, false]) expect(plain(windows.appMenu(actions(), developer))).toEqual(plain(linux.appMenu(actions(), developer)));
    expect(windows.appMenu(actions(), false).map((top) => top.label)).toEqual(["File", "Edit", "View", "Help"]);
  });

  it("draw an .ico in the notification area, by the taskbar's own mode, at each size Windows draws it", async () => {
    const { trayIcon } = await importedOn("win32", () => import("../src/shell/menus.js"));
    expect([trayIcon(false, undefined, true), trayIcon(true, undefined, true)]).toEqual(["tray-dark.ico", "tray-dark.ico"]);
    expect([trayIcon(true, undefined, false), trayIcon(false, "GNOME", false)]).toEqual(["tray-light.ico", "tray-light.ico"]);
    for (const icon of ["tray-dark.ico", "tray-light.ico"]) {
      expect(sizesIn(join(import.meta.dirname, "..", "assets", icon)).sort(), icon).toEqual(["16x16", "20x20", "24x24", "32x32"]);
    }
  });
});

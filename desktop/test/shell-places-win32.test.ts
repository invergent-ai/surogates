// Where the app keeps what it keeps on Windows: everything under the user's local application data, Electron's own data
// too, and its cache beside it; no folder that starts it at login, and no launcher. Local, not roaming: a roaming
// profile would copy the VM's 32 GiB sessions disk at every sign-out.

import { expect, it } from "vitest";

import { WIN32 } from "../src/shell/places-win32.js";
import { importedOn } from "./platforms.js";

const LOCAL = "C:\\Users\\Ana\\AppData\\Local";

it("keeps its state in %LOCALAPPDATA%\\Surogate and its cache in its cache folder, with no autostart folder or launcher", () => {
  const windows = { root: `${LOCAL}\\Surogate`, cache: `${LOCAL}\\Surogate\\cache`, autostartFolder: null, launcher: null };
  expect(WIN32.placesFor({ LOCALAPPDATA: LOCAL }, "C:\\Users\\Ana")).toEqual(windows);
  // Never the roaming folder, and none of Linux's XDG folders: an environment that names them changes nothing.
  expect(WIN32.placesFor({ LOCALAPPDATA: LOCAL, APPDATA: "C:\\Users\\Ana\\AppData\\Roaming", XDG_DATA_HOME: "/data", HOME: "/home/ana" }, "C:\\Users\\Ana")).toEqual(windows);
});

it("is in the folder the environment names, as a test's session names its own, and in the user's profile where it names none, or none whole", () => {
  expect(WIN32.placesFor({ LOCALAPPDATA: "D:\\a\\_temp\\sd-1\\l" }, "C:\\Users\\Ana").root).toBe("D:\\a\\_temp\\sd-1\\l\\Surogate");
  expect(WIN32.placesFor({ LOCALAPPDATA: "D:/a/_temp/sd-1/l/" }, "C:\\Users\\Ana").cache).toBe("D:\\a\\_temp\\sd-1\\l\\Surogate\\cache");
  // Relative, on no drive, or on a network share: the VM's disks could not be kept there.
  for (const env of [{}, { LOCALAPPDATA: "" }, { LOCALAPPDATA: "AppData\\Local" }, { LOCALAPPDATA: "\\Users\\Ana" }, { LOCALAPPDATA: "C:AppData" }, { LOCALAPPDATA: "\\\\server\\profiles\\ana" }]) {
    expect(WIN32.placesFor(env, "C:\\Users\\Ana").root, JSON.stringify(env)).toBe(`${LOCAL}\\Surogate`);
  }
});

it("are the places a Windows app keeps", async () => {
  const { placesFor } = await importedOn("win32", () => import("../src/shell/places.js"));
  expect(placesFor({ LOCALAPPDATA: LOCAL }, "C:\\Users\\Ana")).toEqual(WIN32.placesFor({ LOCALAPPDATA: LOCAL }, "C:\\Users\\Ana"));
});

// Where the app keeps what it keeps on macOS: its state in the user's Application Support, Electron's
// own data too, and its cache in the user's Caches, whatever the environment says; no folder that
// starts it at login, and no launcher.

import { expect, it } from "vitest";

import { DARWIN } from "../src/shell/places-darwin.js";
import { importedOn } from "./platforms.js";

it("keeps its state in ~/Library/Application Support/Surogate and its cache in ~/Library/Caches/Surogate, with no autostart folder or launcher", () => {
  const mac = { root: "/Users/ana/Library/Application Support/Surogate", cache: "/Users/ana/Library/Caches/Surogate", autostartFolder: null, launcher: null };
  expect(DARWIN.placesFor({}, "/Users/ana")).toEqual(mac);
  // The XDG folders are Linux's: a Mac's environment that names them changes nothing.
  expect(DARWIN.placesFor({ XDG_DATA_HOME: "/data", XDG_CONFIG_HOME: "/config", XDG_CACHE_HOME: "/cache" }, "/Users/ana")).toEqual(mac);
});

it("are the places a Mac's app keeps", async () => {
  const { placesFor } = await importedOn("darwin", () => import("../src/shell/places.js"));
  expect(placesFor({}, "/Users/ana")).toEqual(DARWIN.placesFor({}, "/Users/ana"));
});

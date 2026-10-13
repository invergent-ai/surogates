// Where the app keeps what it keeps on macOS: its state in the user's Application Support, Electron's
// own data too, and its cache in the user's Caches, in the home the environment names, or the user's
// own; no folder that starts it at login, and no launcher.

import { expect, it } from "vitest";

import { DARWIN } from "../src/shell/places-darwin.js";
import { importedOn } from "./platforms.js";

it("keeps its state in ~/Library/Application Support/Surogate and its cache in ~/Library/Caches/Surogate, with no autostart folder or launcher", () => {
  const mac = { root: "/Users/ana/Library/Application Support/Surogate", cache: "/Users/ana/Library/Caches/Surogate", autostartFolder: null, launcher: null };
  expect(DARWIN.placesFor({ HOME: "/Users/ana" }, "/Users/ana")).toEqual(mac);
  // The XDG folders are Linux's: a Mac's environment that names them changes nothing.
  expect(DARWIN.placesFor({ HOME: "/Users/ana", XDG_DATA_HOME: "/data", XDG_CONFIG_HOME: "/config", XDG_CACHE_HOME: "/cache" }, "/Users/ana")).toEqual(mac);
});

it("is in the home the environment names, as a test's session names its own, and in the user's where it names none, or none whole", () => {
  expect(DARWIN.placesFor({ HOME: "/tmp/sd-1/h" }, "/Users/ana").root).toBe("/tmp/sd-1/h/Library/Application Support/Surogate");
  expect(DARWIN.placesFor({ HOME: "/tmp/sd-1/h" }, "/Users/ana").cache).toBe("/tmp/sd-1/h/Library/Caches/Surogate");
  for (const env of [{}, { HOME: "" }, { HOME: "h" }]) expect(DARWIN.placesFor(env, "/Users/ana").root).toBe("/Users/ana/Library/Application Support/Surogate");
});

it("are the places a Mac's app keeps", async () => {
  const { placesFor } = await importedOn("darwin", () => import("../src/shell/places.js"));
  expect(placesFor({}, "/Users/ana")).toEqual(DARWIN.placesFor({}, "/Users/ana"));
});

// The platform and architecture a release is built for, and the names its files have on the release
// bucket under desktop/: one target each, and Linux's names as every release before them had.

import { expect, it } from "vitest";

import { forThisOs, notBuilt, notBuiltWords, TARGETS, targetOf } from "../src/platform.js";
import { importedOn } from "./platforms.js";

it("is the target of Linux on x64, of macOS on Apple silicon and of Windows on x64, and of no other computer", () => {
  expect(targetOf("linux", "x64")).toBe("linux-x64");
  expect(targetOf("darwin", "arm64")).toBe("darwin-arm64");
  expect(targetOf("win32", "x64")).toBe("win32-x64");
  expect(targetOf("darwin", "x64")).toBeNull();
  expect(targetOf("linux", "arm64")).toBeNull();
  expect(targetOf("win32", "arm64")).toBeNull();
  // This computer's, by default.
  expect(targetOf()).toBe(targetOf(process.platform, process.arch));
});

it("names Linux's files as every release has named them", () => {
  const linux = TARGETS["linux-x64"];
  expect([linux.platform, linux.arch]).toEqual(["linux", "x64"]);
  expect(linux.archive("1.2.3")).toBe("surogate-desktop-1.2.3-linux-x64.tar.gz");
  expect(linux.releaseDir("1.2.3")).toBe("releases/1.2.3");
  expect(linux.latest).toBe("latest.json");
});

it("names macOS's files in folders of their own, beside Linux's", () => {
  const darwin = TARGETS["darwin-arm64"];
  expect([darwin.platform, darwin.arch]).toEqual(["darwin", "arm64"]);
  expect(darwin.archive("1.2.3")).toBe("surogate-desktop-1.2.3-darwin-arm64.tar.gz");
  expect(darwin.releaseDir("1.2.3")).toBe("releases/1.2.3/darwin-arm64");
  expect(darwin.latest).toBe("darwin-arm64/latest.json");
});

it("names Windows's files in folders of their own, beside Linux's and macOS's, its payload the MSI that installs it", () => {
  const win32 = TARGETS["win32-x64"];
  expect([win32.platform, win32.arch]).toEqual(["win32", "x64"]);
  expect(win32.archive("1.2.3")).toBe("surogate-desktop-1.2.3-win32-x64.msi");
  expect(win32.releaseDir("1.2.3")).toBe("releases/1.2.3/win32-x64");
  expect(win32.latest).toBe("win32-x64/latest.json");
});

it("gives a part its row for the OS it runs on, by one lookup, or null on an OS it has none for", () => {
  const rows = { linux: "Linux's", darwin: "macOS's" };
  expect(forThisOs(rows, "linux")).toBe("Linux's");
  expect(forThisOs(rows, "darwin")).toBe("macOS's");
  expect(forThisOs(rows, "win32")).toBeNull();
  expect(forThisOs({ win32: 0 }, "win32")).toBe(0);
  // This computer's, by default.
  expect(forThisOs({ [process.platform]: "here" })).toBe("here");
});

it("says plainly, of a part that has only Linux's side so far, that it is not built for another OS yet", () => {
  const os = ({ darwin: "macOS", win32: "Windows" } as Partial<Record<NodeJS.Platform, string>>)[process.platform] ?? process.platform;
  expect(() => notBuilt("Surogate Desktop's data folder")).toThrow(`Surogate Desktop's data folder is not built for ${os} yet`);
  expect(notBuiltWords("driving a browser")).toBe(`driving a browser is not built for ${os} yet`);
});

it("says so in the words of the OS it was imported on, as each part picks its side", async () => {
  const there = await importedOn("darwin", () => import("../src/platform.js"));
  expect(() => there.notBuilt("The folder lock")).toThrow(new Error("The folder lock is not built for macOS yet"));
  expect(there.notBuiltWords("driving a browser")).toBe("driving a browser is not built for macOS yet");
  const windows = await importedOn("win32", () => import("../src/platform.js"));
  expect(() => windows.notBuilt("The file host")).toThrow(new Error("The file host is not built for Windows yet"));
  expect(windows.notBuiltWords("driving a browser")).toBe("driving a browser is not built for Windows yet");
  // And an OS of no row of its own by the name Node gives it.
  expect((await importedOn("freebsd", () => import("../src/platform.js"))).notBuiltWords("The VM")).toBe("The VM is not built for freebsd yet");
});

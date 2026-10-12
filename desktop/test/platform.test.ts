// The platform and architecture a release is built for, and the names its files have on the release
// bucket under desktop/: one target each, and Linux's names as every release before them had.

import { expect, it } from "vitest";

import { TARGETS, targetOf } from "../src/platform.js";

it("is the target of Linux on x64 and of macOS on Apple silicon, and of no other computer", () => {
  expect(targetOf("linux", "x64")).toBe("linux-x64");
  expect(targetOf("darwin", "arm64")).toBe("darwin-arm64");
  expect(targetOf("darwin", "x64")).toBeNull();
  expect(targetOf("linux", "arm64")).toBeNull();
  expect(targetOf("win32", "x64")).toBeNull();
  // This computer's, by default.
  expect(targetOf()).toBe(targetOf(process.platform, process.arch));
});

it("names Linux's files as every release has named them", () => {
  const linux = TARGETS["linux-x64"];
  expect([linux.platform, linux.arch]).toEqual(["linux", "x64"]);
  expect(linux.tarball("1.2.3")).toBe("surogate-desktop-1.2.3-linux-x64.tar.gz");
  expect(linux.releaseDir("1.2.3")).toBe("releases/1.2.3");
  expect(linux.latest).toBe("latest.json");
});

it("names macOS's files in folders of their own, beside Linux's", () => {
  const darwin = TARGETS["darwin-arm64"];
  expect([darwin.platform, darwin.arch]).toEqual(["darwin", "arm64"]);
  expect(darwin.tarball("1.2.3")).toBe("surogate-desktop-1.2.3-darwin-arm64.tar.gz");
  expect(darwin.releaseDir("1.2.3")).toBe("releases/1.2.3/darwin-arm64");
  expect(darwin.latest).toBe("darwin-arm64/latest.json");
});

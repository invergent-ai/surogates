// This Mac's name, as the agent and Settings show it: the Computer Name surogate-helper reads, and,
// where it gives none, the host name without ".local"; never one that holds a control character.

import { execFileSync } from "node:child_process";
import { describe, expect, it } from "vitest";

import { nameOf } from "../src/shell/computer-name-darwin.js";

describe("this Mac's name", () => {
  it("is the Computer Name the helper reads, its line's end taken off", () => {
    expect(nameOf(() => "Ana's MacBook Air\n", () => "anas-mba.local")).toBe("Ana's MacBook Air");
    expect(nameOf(() => "Büro – Mac", () => "x.local")).toBe("Büro – Mac");
  });

  it("is the host name without .local where the helper fails, names nothing, or names one with a control character", () => {
    const failing = () => {
      throw new Error("surogate-helper name failed");
    };
    expect(nameOf(failing, () => "anas-mba.local")).toBe("anas-mba");
    expect(nameOf(() => "\n", () => "anas-mba.LOCAL")).toBe("anas-mba");
    expect(nameOf(() => "Ana\u0007s Mac\n", () => "anas-mba.local")).toBe("anas-mba");
    expect(nameOf(() => "Ana\ns Mac\n", () => "anas-mba.local")).toBe("anas-mba");
    expect(nameOf(failing, () => "mac-mini")).toBe("mac-mini");
  });

  it("is the host name as it is where that is all there is", () => {
    expect(nameOf(() => "", () => ".local")).toBe(".local");
  });
});

// The helper started as the app starts it (clean-child.ts).
describe.runIf(process.platform === "darwin")("this Mac's name, read on a Mac", () => {
  it("is the Computer Name of System Settings, as scutil reads it", async () => {
    const { computerName } = await import("../src/shell/computer-name.js");
    expect(computerName()).toBe(execFileSync("/usr/sbin/scutil", ["--get", "ComputerName"], { encoding: "utf8" }).trim());
  });
});

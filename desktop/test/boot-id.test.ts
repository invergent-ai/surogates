// This boot's identity, as each OS names it: a folder's binding records it, so a reboot that renumbers the folder's
// mount is told from a folder put in its place (binding/folder.ts, confirmedFolder).

import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { bootId } from "../src/binding/boot-id.js";
import { BOOT_ID } from "../src/binding/folder.js";
import { importedOn } from "./platforms.js";

describe("this boot's identity", () => {
  it.runIf(process.platform === "linux")("is, on Linux, the kernel's boot id, and what a folder's binding records", () => {
    expect(bootId()).toBe(readFileSync("/proc/sys/kernel/random/boot_id", "utf8").trim());
    expect(bootId()).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
    expect(BOOT_ID).toBe(bootId());
  });

  it.runIf(process.platform === "darwin")("is, on macOS, the kernel's boot session UUID, and what a folder's binding records", () => {
    const uuid = spawnSync("/usr/sbin/sysctl", ["-n", "kern.bootsessionuuid"], { encoding: "utf8" }).stdout.trim();
    expect(uuid).toMatch(/^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$/i);
    expect(bootId()).toBe(uuid);
    expect(BOOT_ID).toBe(bootId());
  });

  it("is nothing on a system whose boot it does not read yet, which counts as this boot", async () => {
    const { bootId: elsewhere } = await importedOn("freebsd", () => import("../src/binding/boot-id.js"));
    expect(elsewhere()).toBe("");
  });
});

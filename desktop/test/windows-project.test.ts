// The files the Windows workflow runs (vitest.config.ts, WINDOWS): a project of their own on Windows alone, so that on
// Linux and macOS each runs once, in the unit project.

import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { importedOn } from "./platforms.js";

interface Project {
  test: { name: string; include?: string[] };
}
const projects = (config: unknown) => (config as { test: { projects: Project[] } }).test.projects;

describe("the Windows workflow's files", () => {
  it("are each a test file there to run, the release target's own among them", async () => {
    const { WINDOWS } = await import("../vitest.config.js");
    expect(WINDOWS.filter((file) => !/^test\/[^/]+\.test\.ts$/.test(file) || !existsSync(fileURLToPath(new URL(`../${file}`, import.meta.url))))).toEqual([]);
    expect(WINDOWS).toContain("test/platform.test.ts");
  });

  it("are a project on Windows alone", async () => {
    for (const os of ["linux", "darwin"] as const) {
      const names = projects((await importedOn(os, () => import("../vitest.config.js"))).default).map(({ test }) => test.name);
      expect(names, os).not.toContain("windows");
    }
    const win32 = await importedOn("win32", () => import("../vitest.config.js"));
    expect(projects(win32.default).map(({ test }) => test.name)).toEqual(["unit", "mounting", "windows"]);
    expect(projects(win32.default).at(-1)?.test.include).toEqual(win32.WINDOWS);
  });
});

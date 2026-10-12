// The files the macOS workflow runs (vitest.config.ts, MACOS): a project of their own on macOS alone, so that on Linux
// each runs once, in the unit project.

import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { importedOn } from "./platforms.js";

interface Project {
  test: { name: string; include?: string[] };
}
const projects = (config: unknown) => (config as { test: { projects: Project[] } }).test.projects;

describe("the macOS workflow's files", () => {
  it("are each a test file there to run", async () => {
    const { MACOS } = await import("../vitest.config.js");
    expect(MACOS.filter((file) => !/^test\/[^/]+\.test\.ts$/.test(file) || !existsSync(fileURLToPath(new URL(`../${file}`, import.meta.url))))).toEqual([]);
  });

  it("are a project on macOS alone", async () => {
    const linux = projects((await importedOn("linux", () => import("../vitest.config.js"))).default);
    const darwin = await importedOn("darwin", () => import("../vitest.config.js"));
    expect(linux.map(({ test }) => test.name)).toEqual(["unit", "mounting"]);
    expect(projects(darwin.default).map(({ test }) => test.name)).toEqual(["unit", "mounting", "macos"]);
    expect(projects(darwin.default).at(-1)?.test.include).toEqual(darwin.MACOS);
  });
});

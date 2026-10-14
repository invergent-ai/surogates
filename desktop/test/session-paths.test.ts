// A session's paths are POSIX on every OS (session-path.ts): the server, the model, the guest and the file host's checks
// name each folder by its path in the guest. This computer's own paths (its data folder, its runtime folder, a program)
// are its OS's (host-path.ts). So no module that handles either takes node:path's functions for this OS by default: each
// says which paths it means.

import * as path from "node:path";
import { readdirSync, readFileSync } from "node:fs";
import { join, posix } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, it } from "vitest";

import * as host from "../src/host-path.js";
import * as session from "../src/session-path.js";
import { importedOn } from "./platforms.js";

const SRC = fileURLToPath(new URL("../src", import.meta.url));
const FOLDERS = ["files", "hosts", "binding", "history", "vm"];

const sources = (dir: string): string[] =>
  readdirSync(join(SRC, dir), { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? sources(posix.join(dir, entry.name)) : entry.name.endsWith(".ts") ? [posix.join(dir, entry.name)] : []);

// The code without its comments, which may name any module.
const code = (text: string) => text.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");

// What *text* imports of node:path, by each import: its names, or "*" for the module whole and "default" for its default.
const fromPath = (text: string): string[][] =>
  [...code(text).matchAll(/\bimport\s+([^;]*?)\s+from\s*["'](?:node:)?path["']|\bimport\s*\(\s*["'](?:node:)?path["']\s*\)|\brequire\s*\(\s*["'](?:node:)?path["']\s*\)/g)].map(([, what]) => {
    if (what === undefined) return ["*"];
    const named = /\{([^}]*)\}/.exec(what)?.[1];
    const names = (named ?? "").split(",").map((name) => name.trim().replace(/^type\s+/, "").split(/\s+as\s+/)[0]!).filter(Boolean);
    return [...names, ...(/\*\s+as\s+/.test(what) ? ["*"] : []), ...(/^\s*[A-Za-z_$][\w$]*\s*(,|$)/.test(what) ? ["default"] : [])];
  });

it("takes no path function of node:path for this OS in a module that handles a session's paths or this computer's", () => {
  const taking = FOLDERS.flatMap(sources).flatMap((file) => {
    const what = fromPath(readFileSync(join(SRC, file), "utf8")).flat();
    return what.filter((name) => name !== "posix" && name !== "win32").map((name) => `${file}: ${name}`);
  });
  expect(taking).toEqual([]);
});

it("finds node:path taken by any form of import", () => {
  expect(fromPath('import { dirname, join, resolve } from "node:path";')).toEqual([["dirname", "join", "resolve"]]);
  expect(fromPath('import { posix } from "node:path";\nimport { win32 as windows } from "path";')).toEqual([["posix"], ["win32"]]);
  expect(fromPath('import * as path from "node:path";')).toEqual([["*"]]);
  expect(fromPath('import path, { posix } from "node:path";')).toEqual([["posix", "default"]]);
  expect(fromPath('import type { ParsedPath } from "node:path";')).toEqual([["ParsedPath"]]);
  expect(fromPath('const path = await import("node:path");')).toEqual([["*"]]);
  expect(fromPath('// import { join } from "node:path";')).toEqual([]);
});

it("names a session's paths by POSIX's functions on every OS, and this computer's by its OS's own", () => {
  for (const name of ["join", "dirname", "basename", "relative", "isAbsolute", "normalize", "resolve", "sep"] as const) {
    expect(session[name], name).toBe(posix[name]);
    expect(host[name], name).toBe(path[name]);
  }
  expect(host.delimiter).toBe(path.delimiter);
});

it("gives a folder's path in the session as the folder's own on Linux and macOS, either way (Windows's: session-path-win32.test.ts)", async () => {
  for (const os of ["linux", "darwin"] as const) {
    const there = await importedOn(os, () => import("../src/session-path.js"));
    expect(there.guestPathOf("/home/ana/Documents/shop"), os).toBe("/home/ana/Documents/shop");
    expect(there.hostPathOf("/Users/ana/My folder's"), os).toBe("/Users/ana/My folder's");
  }
});

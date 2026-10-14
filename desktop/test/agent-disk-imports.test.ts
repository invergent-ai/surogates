// The guest's agent disk carries only some of the app's modules (vm/agent-disk.sh): the guest agent, the file kinds
// and the link protocol's outcomes. Each of them imports nothing but the others and Node's own, wherever it runs:
// the guest has no platform.js, no node_modules, and nothing else of the app's. A module of them that loads a file
// beside the app's code, by its URL, loads it only on an OS the guest is not.

import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, posix, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
const SRC = join(DESKTOP, "src");

// What the disk carries, as its script copies it out of dist/: folders whole, and single modules.
function carried(): { folders: string[]; modules: string[] } {
  const script = readFileSync(join(DESKTOP, "vm", "agent-disk.sh"), "utf8");
  const copies = [...script.matchAll(/^cp (-r )?(.+)$/gm)].filter(([line]) => line.includes("$DESKTOP/dist/"));
  const named = (words: string) => [...words.matchAll(/"\$DESKTOP\/dist\/([^"]+)"/g)].map(([, path]) => path!);
  return {
    folders: copies.filter(([, whole]) => whole).flatMap(([, , words]) => named(words!)),
    modules: copies.filter(([, whole]) => !whole).flatMap(([, , words]) => named(words!)).map((path) => path.replace(/\.js$/, ".ts")),
  };
}

const sources = (dir: string): string[] =>
  readdirSync(join(SRC, dir), { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? sources(posix.join(dir, entry.name)) : entry.name.endsWith(".ts") ? [posix.join(dir, entry.name)] : []);

// The code without its comments, which may name any module.
const code = (text: string) => text.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");

// Each module *text* imports or exports from, statically or as a dynamic import.
const imported = (text: string): string[] =>
  [...code(text).matchAll(/(?:\bfrom\s*|\bimport\s*\(\s*|^\s*import\s+)["']([^"']+)["']/gm)].map(([, spec]) => spec!);

// Each file *text* reaches by a URL beside its own module.
const loaded = (text: string): string[] => [...code(text).matchAll(/new URL\(\s*["']([^"']+)["']\s*,\s*import\.meta\.url\s*\)/g)].map(([, spec]) => spec!);

describe("the guest's agent disk", () => {
  const disk = carried();
  const onDisk = (file: string) => disk.folders.some((folder) => file.startsWith(`${folder}/`)) || disk.modules.includes(file);
  const modules = [...disk.folders.flatMap(sources), ...disk.modules];

  it("is the agent, the file kinds and the link protocol's outcomes, as its script copies them", () => {
    expect(disk).toEqual({ folders: ["guest", "files"], modules: ["link/protocol.ts"] });
  });

  it("holds no module that imports anything but another on it and Node's own", () => {
    const outside = modules.flatMap((file) => imported(readFileSync(join(SRC, file), "utf8")).flatMap((spec) => {
      if (spec.startsWith("node:")) return [];
      if (!spec.startsWith(".")) return [`${file}: ${spec}`];
      const target = relative(SRC, join(SRC, dirname(file), spec)).replace(/\.js$/, ".ts");
      return onDisk(target) ? [] : [`${file}: ${spec}`];
    }));
    expect(outside).toEqual([]);
  });

  it("finds a module of its own by every import its modules make", () => {
    const missing = modules.flatMap((file) => imported(readFileSync(join(SRC, file), "utf8")).filter((spec) => spec.startsWith("."))
      .map((spec) => relative(SRC, join(SRC, dirname(file), spec)).replace(/\.js$/, ".ts")).filter((target) => !modules.includes(target)));
    expect(missing).toEqual([]);
  });

  it("loads nothing beside the app's code but fsat and ntat, which the file host loads on macOS and on Windows alone", () => {
    const beside = modules.flatMap((file) => loaded(readFileSync(join(SRC, file), "utf8")).map((spec) => `${file}: ${spec}`));
    expect(beside).toEqual(["files/fsat.ts: ../../dist/native/fsat.node", "files/ntat.ts: ../../dist/native/ntat.node"]);
    const flags = readFileSync(join(SRC, "files", "flags.ts"), "utf8");
    expect(code(flags)).toMatch(/process\.platform === "darwin" \? fsat\(\)\.constants : null/);
  });
});

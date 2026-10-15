// scripts/layout-digest.sh, the one word a release's build job says of the layout it hands on as an artifact, and that
// the job that signs it checks first: the same for the same files whatever order they were made in, and another for any
// file's change, name, coming or going; blind to what an artifact does not carry, and refusing a link.

import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, expect, it } from "vitest";

const SCRIPT = fileURLToPath(new URL("../scripts/layout-digest.sh", import.meta.url));
let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "layout-digest-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

const digest = (...args: string[]) => spawnSync("bash", [SCRIPT, ...args], { encoding: "utf8" });
// A layout at *dir*/*name* of *files*, each made in the order given.
const layout = (name: string, files: Array<[string, string]>) => {
  const top = join(dir, name);
  for (const [path, content] of files) {
    mkdirSync(join(top, path, ".."), { recursive: true });
    writeFileSync(join(top, path), content);
  }
  return top;
};
const FILES: Array<[string, string]> = [["Surogate.exe", "a program"], ["resources/app/package.json", "{}"], ["resources/app/dist/main.js", "code"], ["bin/surogate-vm.exe", "a program too"]];

it("says one sha256 of a layout, the same for the same files whatever order they were made in", () => {
  const first = digest(layout("first", FILES));
  expect(first).toMatchObject({ status: 0, stderr: "" });
  expect(first.stdout).toMatch(/^[0-9a-f]{64}\n$/);
  expect(digest(layout("second", [...FILES].reverse())).stdout).toBe(first.stdout);
});

it("says another for a file's other bytes, another name, a file more or a file less", () => {
  const word = digest(layout("base", FILES)).stdout;
  const changes: Array<[string, (top: string) => void]> = [
    ["other bytes", (top) => writeFileSync(join(top, "resources", "app", "dist", "main.js"), "other code")],
    ["another name", (top) => renameSync(join(top, "Surogate.exe"), join(top, "surogate.exe"))],
    ["a file moved", (top) => renameSync(join(top, "bin", "surogate-vm.exe"), join(top, "surogate-vm.exe"))],
    ["a file more", (top) => writeFileSync(join(top, "bin", "more.dll"), "more")],
    ["a file less", (top) => rmSync(join(top, "resources", "app", "package.json"))],
    ["an empty file more", (top) => writeFileSync(join(top, "empty"), "")],
  ];
  for (const [name, change] of changes) {
    const top = layout(name.replaceAll(" ", "-"), FILES);
    change(top);
    expect([name, digest(top).stdout]).not.toEqual([name, word]);
  }
});

it("leaves out what an artifact does not carry, a file or folder whose name starts with a dot, and an empty folder", () => {
  const word = digest(layout("base", FILES)).stdout;
  const top = layout("hidden", FILES);
  writeFileSync(join(top, "resources", "app", ".package-lock.json"), "{}");
  mkdirSync(join(top, "resources", "app", ".bin"));
  writeFileSync(join(top, "resources", "app", ".bin", "tool.cmd"), "a shim");
  mkdirSync(join(top, "empty"));
  expect(digest(top).stdout).toBe(word);
});

it("refuses a layout that holds a link, and is told one folder", () => {
  const top = layout("linked", FILES);
  symlinkSync("Surogate.exe", join(top, "bin", "Surogate.exe"));
  expect(digest(top)).toMatchObject({ status: 1, stdout: "", stderr: `layout-digest.sh: ${top} holds a link, bin/Surogate.exe\n` });
  for (const args of [[], [join(dir, "none")], [top, top]]) expect(digest(...args), args.join(" ")).toMatchObject({ status: 2, stdout: "", stderr: "usage: scripts/layout-digest.sh <folder>\n" });
});

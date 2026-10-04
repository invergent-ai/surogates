import { mkdirSync, mkdtempSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { checkFolder, type FolderGuards } from "../src/binding/folder.js";

let base: string;
let guards: FolderGuards;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "folder-")));
  mkdirSync(join(base, "home"));
  guards = { home: join(base, "home"), dataDir: join(base, "data"), appDirs: [join(base, "app")] };
});

afterEach(() => {
  rmSync(base, { recursive: true, force: true });
});

describe("a chat's folder", () => {
  it("is the folder as it resolves, with its device and inode", () => {
    mkdirSync(join(base, "notes"));
    symlinkSync(join(base, "notes"), join(base, "link"));
    const { dev, ino } = statSync(join(base, "notes"));
    expect(checkFolder(join(base, "link"), guards)).toEqual({ ok: true, path: join(base, "notes"), dev, ino });
  });

  it("is missing when it is not there, is not a folder, or has a name that is not valid UTF-8", () => {
    writeFileSync(join(base, "a.txt"), "");
    // How a dialog would hand over a name that is not valid UTF-8: decoded, with U+FFFD.
    mkdirSync(Buffer.from([...Buffer.from(`${base}/bad-`), 0xff]));
    for (const folder of [join(base, "gone"), join(base, "a.txt"), join(base, "bad-�")]) {
      expect(checkFolder(folder, guards)).toMatchObject({ ok: false, missing: true });
    }
  });

  it.each([
    ["the whole system", () => "/", /home folder or the app's own data/],
    ["the home folder", () => join(base, "home"), /home folder or the app's own data/],
    ["a folder that holds the home folder", () => base, /home folder or the app's own data/],
    ["the app's data", () => join(base, "data"), /home folder or the app's own data/],
    ["a folder inside the app's files", () => join(base, "app", "sub"), /home folder or the app's own data/],
    ["a credential folder", () => join(base, "home", ".config", "gh"), /home folder or the app's own data/],
    ["a path srt would read as a glob", () => join(base, "x[ab]"), /holds \*, \?, \[ or \]/],
    ["a system folder", () => "/proc", /system folders/],
  ])("may never be %s", (_name, folder, message) => {
    for (const dir of ["data", "app/sub", "home/.config/gh", "x[ab]"]) mkdirSync(join(base, dir), { recursive: true });
    const checked = checkFolder(folder(), guards);
    expect(checked).toMatchObject({ ok: false, missing: false });
    expect(!checked.ok && checked.message).toMatch(message);
  });

  it("may not be where a credential folder's link leads", () => {
    mkdirSync(join(base, "dotfiles"));
    symlinkSync(join(base, "dotfiles"), join(base, "home", ".ssh"));
    expect(checkFolder(join(base, "dotfiles"), guards)).toMatchObject({ ok: false, missing: false });
  });
});

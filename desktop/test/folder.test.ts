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
  guards = { home: join(base, "home"), dataDir: join(base, "data"), cacheDir: join(base, "home", ".cache", "surogate"), appDirs: [join(base, "app")] };
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
    for (const folder of [join(base, "gone"), join(base, "a.txt"), join(base, "bad-\uFFFD")]) {
      expect(checkFolder(folder, guards)).toMatchObject({ ok: false, missing: true });
    }
  });

  it.each([
    ["the whole system", () => "/", /home folder or the app's own data/],
    ["the home folder", () => join(base, "home"), /home folder or the app's own data/],
    ["a folder that holds the home folder", () => base, /home folder or the app's own data/],
    ["the app's data", () => join(base, "data"), /home folder or the app's own data/],
    ["the folder in the app's data that its sandbox's image is downloaded into", () => join(base, "data", "vm", "images"), /home folder or the app's own data/],
    ["the app's cache, where it downloads an update", () => join(base, "home", ".cache", "surogate"), /home folder or the app's own data/],
    ["a folder inside the app's cache", () => join(base, "home", ".cache", "surogate", "updates"), /home folder or the app's own data/],
    ["the user's cache, which holds the app's", () => join(base, "home", ".cache"), /home folder or the app's own data/],
    ["a folder inside the app's files", () => join(base, "app", "sub"), /home folder or the app's own data/],
    ["a credential folder", () => join(base, "home", ".config", "gh"), /home folder or the app's own data/],
    ["a path srt would read as a glob", () => join(base, "x[ab]"), /holds \*, \?, \[ or \]/],
    ["a system folder", () => "/proc", /system folders/],
    ["a folder of the sandbox's tools", () => "/usr/share", /the sandbox keeps its own tools in \/usr, so the folder \/usr\/share cannot be a chat's$/],
    ["a folder that holds them", () => "/var", /the sandbox keeps its own tools in \/var\/cache, so the folder \/var cannot be a chat's$/],
  ])("may never be %s", (_name, folder, message) => {
    for (const dir of ["data/vm/images", "app/sub", "home/.config/gh", "home/.cache/surogate/updates", "x[ab]"]) mkdirSync(join(base, dir), { recursive: true });
    const checked = checkFolder(folder(), guards);
    expect(checked).toMatchObject({ ok: false, missing: false });
    expect(!checked.ok && checked.message).toMatch(message);
  });

  it("may be beside the sandbox's tools, where a chat's folder hides none of them", () => {
    const beside = realpathSync(mkdtempSync("/var/tmp/folder-"));
    try {
      expect(checkFolder(beside, guards)).toMatchObject({ ok: true, path: beside });
    } finally {
      rmSync(beside, { recursive: true, force: true });
    }
  });

  it("may be another program's folder in the user's cache, beside the app's", () => {
    mkdirSync(join(base, "home", ".cache", "surogate"), { recursive: true });
    mkdirSync(join(base, "home", ".cache", "pip"));
    expect(checkFolder(join(base, "home", ".cache", "pip"), guards)).toMatchObject({ ok: true, path: join(base, "home", ".cache", "pip") });
  });

  it("may not hold the app's cache, or lie in it, where the user's cache is a link, as one moved to another disk", () => {
    mkdirSync(join(base, "disk", "cache", "surogate", "updates"), { recursive: true });
    symlinkSync(join(base, "disk", "cache"), join(base, "home", ".cache"));
    for (const folder of [join(base, "disk", "cache"), join(base, "disk", "cache", "surogate"), join(base, "disk", "cache", "surogate", "updates"), join(base, "disk")]) {
      const checked = checkFolder(folder, guards);
      expect(checked, folder).toMatchObject({ ok: false, missing: false });
      expect(!checked.ok && checked.message).toMatch(/home folder or the app's own data/);
    }
    // A cache folder that is not there yet is still where its home's link leads.
    rmSync(join(base, "disk", "cache", "surogate"), { recursive: true });
    expect(checkFolder(join(base, "disk", "cache"), guards)).toMatchObject({ ok: false, missing: false });
  });

  it("may not hold a link that the way to the app's cache or its data goes through, wherever that link leads now: a command could point it elsewhere", () => {
    // The cache home a link to a folder that is reached through a second link, in a folder beside both.
    mkdirSync(join(base, "b", "real", "cache"), { recursive: true });
    mkdirSync(join(base, "a"));
    symlinkSync(join(base, "b", "real"), join(base, "a", "link"));
    symlinkSync(join(base, "a", "link", "cache"), join(base, "home", ".cache"));
    const checked = checkFolder(join(base, "a"), guards);
    expect(checked).toMatchObject({ ok: false, missing: false });
    expect(!checked.ok && checked.message).toMatch(/home folder or the app's own data/);
    // A folder beside the link, and one the way does not go through, may be a chat's.
    mkdirSync(join(base, "a2"));
    mkdirSync(join(base, "b", "other"));
    expect(checkFolder(join(base, "a2"), guards)).toMatchObject({ ok: true });
    expect(checkFolder(join(base, "b", "other"), guards)).toMatchObject({ ok: true });
  });

  it("may not be where a credential folder's link leads", () => {
    mkdirSync(join(base, "dotfiles"));
    symlinkSync(join(base, "dotfiles"), join(base, "home", ".ssh"));
    expect(checkFolder(join(base, "dotfiles"), guards)).toMatchObject({ ok: false, missing: false });
  });
});

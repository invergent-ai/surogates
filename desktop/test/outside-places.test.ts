// Folders outside a chat's folder: which real folder a known name means, which may be granted, and which grant
// holds a path, no link followed out of it.

import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { FolderGuards } from "../src/binding/folder.js";
import { fdPaths } from "../src/files/fd-paths.js";
import { grantable, KNOWN_FOLDERS, knownFolder, withinGrant } from "../src/outside/places.js";

let base: string;
let guards: FolderGuards;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "outside-")));
  for (const dir of ["home/.ssh", "home/Desktop/Scan", "home/Desktop/PONTAJE", "home/Desk", "data", "work"]) mkdirSync(join(base, dir), { recursive: true });
  guards = { home: join(base, "home"), dataDir: join(base, "data"), cacheDir: join(base, "home", ".cache", "surogate"), appDirs: [join(base, "app")] };
});

afterEach(() => {
  rmSync(base, { recursive: true, force: true });
});

describe("known folders", () => {
  it("resolves known folders through the OS's own paths, whatever case the agent writes", () => {
    const getPath = (name: string) => ({ desktop: "/home/a/OneDrive/Desktop", downloads: "/home/a/Downloads" })[name] ?? `/home/a/${name}`;
    expect(KNOWN_FOLDERS).toEqual(["Desktop", "Downloads", "Documents", "Pictures"]);
    expect(knownFolder("desktop", getPath)).toBe("/home/a/OneDrive/Desktop");
    expect(knownFolder("DESKTOP", getPath)).toBe("/home/a/OneDrive/Desktop");
    expect(knownFolder("Downloads", getPath)).toBe("/home/a/Downloads");
    expect(knownFolder("Music", getPath)).toBeNull();
    expect(knownFolder("/home/a/Desktop", getPath)).toBeNull();
  });
});

describe("a grant", () => {
  it("refuses as a grant every folder checkFolder refuses", () => {
    const work = join(base, "work");
    for (const folder of [join(base, "home"), base, join(base, "home", ".ssh"), join(base, "data")]) {
      const granted = grantable(folder, guards, work);
      expect(granted, folder).toMatchObject({ ok: false });
      expect(granted.ok ? "" : granted.message, folder).not.toBe("");
    }
    expect(grantable(join(base, "home", "Desktop"), guards, work)).toEqual({ ok: true, folder: join(base, "home", "Desktop") });
  });

  it("refuses a grant of the chat's own folder", () => {
    const work = join(base, "work");
    symlinkSync(work, join(base, "home", "work-link"));
    expect(grantable(work, guards, work)).toEqual({ ok: false, message: "That is this chat's folder already." });
    expect(grantable(join(base, "home", "work-link"), guards, work)).toEqual({ ok: false, message: "That is this chat's folder already." });
    // The folder holding the chat's folder, as Diana's Desktop holds her chat's Scan, may be granted.
    expect(grantable(base, guards, join(base, "home", "Desktop", "Scan"))).toMatchObject({ ok: false });
    expect(grantable(join(base, "home", "Desktop"), guards, join(base, "home", "Desktop", "Scan"))).toMatchObject({ ok: true });
  });
});

describe("the grant holding a path", () => {
  it("finds the grant that holds a path", () => {
    const desktop = join(base, "home", "Desktop");
    const scan = join(desktop, "Scan");
    writeFileSync(join(scan, "cerere avans.pdf"), "pdf");
    writeFileSync(join(desktop, "note.txt"), "note");
    const grants = [desktop, scan];
    expect(withinGrant(grants, join(scan, "cerere avans.pdf"), fdPaths)).toBe(scan);
    expect(withinGrant(grants, scan, fdPaths)).toBe(scan);
    expect(withinGrant(grants, join(desktop, "note.txt"), fdPaths)).toBe(desktop);
    expect(withinGrant(grants, join(desktop, "PONTAJE"), fdPaths)).toBe(desktop);
    // Not there yet: nothing on its way is a link, so the grant still holds it.
    expect(withinGrant(grants, join(desktop, "PONTAJE", "new.pdf"), fdPaths)).toBe(desktop);
    // A folder whose name only starts with a grant's is not in it, nor is the folder above.
    expect(withinGrant([join(base, "home", "Desk")], join(desktop, "note.txt"), fdPaths)).toBeNull();
    expect(withinGrant(grants, join(base, "home"), fdPaths)).toBeNull();
    // A path that is not in its plain form is in no grant: .. after a link would be the OS's to resolve through it.
    expect(withinGrant(grants, `${desktop}/../.ssh`, fdPaths)).toBeNull();
    expect(withinGrant(grants, `${scan}/../note.txt`, fdPaths)).toBeNull();
    expect(withinGrant(grants, `${desktop}/./note.txt`, fdPaths)).toBeNull();
    expect(withinGrant(grants, "Desktop/note.txt", fdPaths)).toBeNull();
    // A grant whose folder is gone holds nothing.
    expect(withinGrant([join(base, "gone")], join(base, "gone", "x"), fdPaths)).toBeNull();
  });

  it("refuses a path that leads out of the grant through a link", () => {
    const desktop = join(base, "home", "Desktop");
    const elsewhere = join(base, "elsewhere");
    mkdirSync(elsewhere);
    writeFileSync(join(elsewhere, "secret.txt"), "secret");
    symlinkSync(elsewhere, join(desktop, "elsewhere"));
    symlinkSync(join(base, "home", ".ssh"), join(desktop, "keys"));
    symlinkSync(join(elsewhere, "secret.txt"), join(desktop, "secret.txt"));
    expect(withinGrant([desktop], join(desktop, "elsewhere"), fdPaths)).toBeNull();
    expect(withinGrant([desktop], join(desktop, "elsewhere", "secret.txt"), fdPaths)).toBeNull();
    expect(withinGrant([desktop], join(desktop, "keys", "id_ed25519"), fdPaths)).toBeNull();
    expect(withinGrant([desktop], join(desktop, "secret.txt"), fdPaths)).toBeNull();
    // A grant whose own folder was swapped for a link since holds nothing.
    rmSync(join(desktop, "PONTAJE"), { recursive: true });
    symlinkSync(elsewhere, join(desktop, "PONTAJE"));
    expect(withinGrant([join(desktop, "PONTAJE")], join(desktop, "PONTAJE", "secret.txt"), fdPaths)).toBeNull();
  });
});

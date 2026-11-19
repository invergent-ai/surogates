// Folders outside a chat's folder: which real folder a known name means, which may be granted, and which grant
// holds a path, no link followed out of it.

import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { FolderGuards } from "../src/binding/folder.js";
import { type FdPaths, fdPaths } from "../src/files/fd-paths.js";
import { grantable, KNOWN_FOLDERS, knownFolder, withinGrant } from "../src/outside/places.js";
import { importedOn } from "./platforms.js";

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

  it("names no folder for a known folder Windows keeps on no drive", async () => {
    const places = await importedOn("win32", () => import("../src/outside/places.js"));
    const getPath = (name: string) => (name === "desktop" ? "\\\\server\\share\\Ana\\Desktop" : `C:\\Users\\Ana\\${name}`);
    expect(places.knownFolder("Desktop", getPath)).toBeNull();
    expect(places.knownFolder("downloads", getPath)).toBe("/mnt/c/Users/Ana/downloads");
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
    // A folder in the chat's folder, which its commands can write, is refused too.
    mkdirSync(join(work, "in", "Documents"), { recursive: true });
    expect(grantable(join(work, "in", "Documents"), guards, work)).toEqual({
      ok: false, message: "That folder is in this chat's folder, which the chat reads already.",
    });
    expect(grantable(base, guards, join(base, "home", "Desktop", "Scan"))).toMatchObject({ ok: false });
    // The folder holding the chat's folder, as Diana's Desktop holds her chat's Scan, may be granted.
    expect(grantable(join(base, "home", "Desktop"), guards, join(base, "home", "Desktop", "Scan"))).toMatchObject({ ok: true });
  });
});

describe("the grant holding a path", () => {
  it("finds a grant for a path spelled with other capitals only where the drive takes it for that very folder", () => {
    const desktop = join(base, "home", "Desktop");
    writeFileSync(join(desktop, "a.pdf"), "pdf");
    const lower = join(base, "home", "desktop");
    // On Linux "desktop" is another name, here none at all: no grant holds it.
    expect(withinGrant([desktop], join(lower, "a.pdf"), fdPaths)).toBeNull();
    // On a drive that ignores case (Windows, macOS) it is the very folder: as such a drive answers, it is the grant's.
    const ignoringCase: FdPaths = { ...fdPaths, lstat: ((path: string, ...rest: unknown[]) =>
      (fdPaths.lstat as (...a: unknown[]) => unknown)(typeof path === "string" && path.toLowerCase().startsWith(lower.toLowerCase())
        ? desktop + path.slice(lower.length) : path, ...rest)) as FdPaths["lstat"] };
    expect(withinGrant([desktop], join(lower, "a.pdf"), ignoringCase)).toBe(desktop);
    // A folder that only starts like it, in any case, is still no grant's.
    expect(withinGrant([desktop], join(base, "home", "desktop2", "a.pdf"), ignoringCase)).toBeNull();
    // On Windows by the names alone: its file host answers a folder by the spelling it was bound by.
    expect(withinGrant([desktop], join(lower, "a.pdf"), fdPaths, true)).toBe(desktop);
    expect(withinGrant([desktop], join(base, "home", "desktop2", "a.pdf"), fdPaths, true)).toBeNull();
  });

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

  it("refuses a path whose grant a link above it now leads elsewhere", () => {
    // A grant made while the way to it was plain; then a folder above it is swapped for a link to the home folder.
    const grant = join(base, "work", "in", "Documents");
    mkdirSync(grant, { recursive: true });
    mkdirSync(join(base, "home", "Documents"));
    writeFileSync(join(base, "home", "Documents", "x"), "home's");
    expect(withinGrant([grant], join(grant, "x"), fdPaths)).toBe(grant);
    rmSync(join(base, "work", "in"), { recursive: true });
    symlinkSync(join(base, "home"), join(base, "work", "in"));
    expect(withinGrant([grant], join(grant, "x"), fdPaths)).toBeNull();
    expect(withinGrant([grant], grant, fdPaths)).toBeNull();
  });

  it("refuses a path in a grant the file host does not hold, as Windows answers for a folder it never bound", () => {
    // Windows's file host, with only the chat's Scan bound: above it a folder with nothing more of it, beside it nothing,
    // and no hold of either (fd-paths-win32.ts, located and within).
    const desktop = join(base, "home", "Desktop");
    const scan = join(desktop, "Scan");
    const missing = () => Object.assign(new Error("ENOENT"), { code: "ENOENT" });
    const above = (path: string) => scan.startsWith(`${path}/`);
    const windows = {
      ...fdPaths,
      lstat: (where: string) => {
        if (above(where)) return fdPaths.lstat(desktop);
        if (where === scan || where.startsWith(`${scan}/`)) return fdPaths.lstat(where);
        throw missing();
      },
      hold: (where: string) => {
        if (above(where)) throw Object.assign(new Error("EACCES"), { code: "EACCES" });
        if (where === scan) return fdPaths.hold(where);
        throw missing();
      },
    } as FdPaths;
    expect(withinGrant([desktop], join(desktop, "junction", "secret.txt"), windows)).toBeNull();
    expect(withinGrant([desktop], join(desktop, "note.txt"), windows)).toBeNull();
    expect(withinGrant([scan], join(scan, "new.pdf"), windows)).toBe(scan);
  });
});

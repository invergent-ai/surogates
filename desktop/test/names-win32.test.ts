// Windows's names, as the file tools refuse them there (src/files/names-win32.ts): what Windows does not open as the
// name itself. The walk's own check of each name, in ntat, is held to the same answers on Windows (ntat-win32.test.ts).

import { describe, expect, it } from "vitest";

import { explorerActsOn } from "../src/files/explorer-names.js";
import { nameRefusal, refusedName, windowsTakes } from "../src/files/names-win32.js";
import { explorerRefusal, writeRefusal } from "../src/files/protect.js";
import { importedOn } from "./platforms.js";
import { REFUSED, TAKEN } from "./windows-names.js";

describe("Windows's names", () => {
  it("take each name Windows opens as itself", () => {
    expect(TAKEN.filter((name) => !windowsTakes(name))).toEqual([]);
  });

  it("refuse each name Windows aliases or does not take: streams, forbidden characters, trailing dots and spaces, short names and devices", () => {
    expect(REFUSED.filter((name) => windowsTakes(name))).toEqual([]);
  });

  it("are refused by the first such name in a path, in the file tools' words", () => {
    expect(refusedName("src/.git./hooks/pre-commit")).toBe(".git.");
    expect(refusedName("a/GIT~1/hooks/x")).toBe("GIT~1");
    expect(refusedName("a/b/c.txt")).toBeNull();
    expect(refusedName("")).toBeNull();
    expect(nameRefusal("CON")).toBe("This name cannot be used on Windows: CON");
  });
});

describe("the names Explorer acts on", () => {
  const folder = "/mnt/c/Users/ana/shop";
  const written = ["desktop.ini", "sub/Desktop.INI", "autorun.inf", "x.lnk", "a/b.URL", "c.scf", "Documents.library-ms", "s.searchconnector-ms"];

  it("are refused to a write in a folder on Windows, in their own words, and are a name like any other on Linux and macOS", async () => {
    const windows = await importedOn("win32", () => import("../src/files/protect.js"));
    for (const name of written) {
      expect(explorerActsOn(name), name).toBe(true);
      expect(windows.writeRefusal(folder, `${folder}/${name}`), name).toBe(windows.explorerRefusal(`${folder}/${name}`));
      expect(writeRefusal(folder, `${folder}/${name}`), name).toBeNull();
    }
    const darwin = await importedOn("darwin", () => import("../src/files/protect.js"));
    expect(written.map((name) => darwin.writeRefusal(folder, `${folder}/${name}`))).toEqual(written.map(() => null));
    expect(explorerRefusal("/x/desktop.ini")).toBe("Write denied: '/x/desktop.ini' is a name Windows's Explorer acts on when it shows this folder.");
  });

  it("leave a name that only holds one, and a folder named so, to Windows as any other", async () => {
    const windows = await importedOn("win32", () => import("../src/files/protect.js"));
    for (const name of ["desktop.ini.bak", "x.lnkx", "desktop.ini/notes.txt", "sub.desktop.inix"]) expect(windows.writeRefusal(folder, `${folder}/${name}`), name).toBeNull();
    // The folder itself, and what lies outside it, are not its names to refuse.
    expect(windows.writeRefusal(`${folder}/x.lnk`, `${folder}/x.lnk`)).toBeNull();
  });

  it("refuse a protected name in its own words first, as on every OS", async () => {
    const windows = await importedOn("win32", () => import("../src/files/protect.js"));
    expect(windows.writeRefusal(folder, `${folder}/.git/hooks/pre-commit`)).toMatch(/is protected in this folder/);
  });
});

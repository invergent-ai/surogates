// A chat's folder on Windows, as the app binds and looks at it outside the file host's walk (binding/folder.ts,
// binding/links.ts, shell/folders-held.ts): named by its path in the guest, /mnt/<drive>/…, its identity read through
// ntat as the walk reads it, and its guards compared by those paths. On Windows alone, with ntat built
// (native/ntat/build.ps1).

import { execFileSync } from "node:child_process";
import { closeSync, linkSync, mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BOOT_ID, checkFolder, type FolderGuards } from "../src/binding/folder.js";
import { scanLinks } from "../src/binding/links.js";
import { identityFrom } from "../src/files/fd-paths-win32.js";
import { ntat } from "../src/files/ntat.js";
import { guestPathOf } from "../src/session-path.js";
import { FolderLooks } from "../src/shell/folders-held.js";

const junction = (link: string, to: string) => execFileSync(join(process.env.SystemRoot ?? "C:\\Windows", "System32", "cmd.exe"), ["/d", "/c", "mklink", "/J", link, to]);

// The identity of the folder *path*, as the walk reads it: ntat's own open of it, its FILE_ID_INFO.
function walkedIdentity(path: string): { dev: number; ino: number } {
  const fd = ntat().openRoot(`\\\\?\\${path}`);
  try {
    return identityFrom(ntat().query(fd));
  } finally {
    closeSync(fd);
  }
}

describe.runIf(process.platform === "win32")("a chat's folder on Windows", () => {
  let base: string;
  let guards: FolderGuards;

  beforeEach(() => {
    base = realpathSync.native(mkdtempSync(join(tmpdir(), "bind-")));
    for (const name of ["home", "data", "cache", "app", "work"]) mkdirSync(join(base, name));
    guards = { home: join(base, "home"), dataDir: join(base, "data"), cacheDir: join(base, "cache"), appDirs: [join(base, "app")] };
  });

  afterEach(() => {
    rmSync(base, { recursive: true, force: true, maxRetries: 10 });
  });

  it("is named by its path in the guest, and known by its identity as the walk reads it, however its path is spelled", () => {
    const work = join(base, "work");
    const guest = guestPathOf(work);
    expect(guest).toMatch(/^\/mnt\/[a-z]\//);
    expect(checkFolder(guest, guards)).toEqual({ ok: true, path: guest, ...walkedIdentity(work) });
    // Through a junction, and in another case, it is the folder as it resolves.
    junction(join(base, "link"), work);
    expect(checkFolder(guestPathOf(join(base, "link")), guards)).toEqual({ ok: true, path: guest, ...walkedIdentity(work) });
    expect(checkFolder(guest.replace(/work$/, "WORK"), guards)).toMatchObject({ ok: true, path: guest });
  });

  it("is refused where it holds the home folder or the app's data, or lies in them, and is not there where no folder is", () => {
    expect(checkFolder(guestPathOf(base), guards)).toMatchObject({ ok: false, missing: false, message: expect.stringContaining("holds this computer's home folder or the app's own data") });
    mkdirSync(join(base, "data", "inside"));
    expect(checkFolder(guestPathOf(join(base, "data", "inside")), guards)).toMatchObject({ ok: false, missing: false });
    expect(checkFolder(guestPathOf(join(base, "nothing")), guards)).toMatchObject({ ok: false, missing: true });
    writeFileSync(join(base, "file"), "");
    expect(checkFolder(guestPathOf(join(base, "file")), guards)).toMatchObject({ ok: false, missing: true, message: expect.stringContaining("is not a folder") });
    // A path the guest has none of: no folder of a chat's.
    expect(checkFolder("\\\\server\\share", guards)).toMatchObject({ ok: false });
  });

  it("is looked at by its path in the guest: held by its identity, and its files linked from outside it found", async () => {
    const work = join(base, "work");
    expect(await new FolderLooks().held([guestPathOf(work)])).toEqual([walkedIdentity(work)]);
    writeFileSync(join(base, "outside.txt"), "shared");
    linkSync(join(base, "outside.txt"), join(work, "inside.txt"));
    expect(await scanLinks(guestPathOf(work))).toEqual({ count: 1, examples: ["inside.txt"], complete: true });
  });

});

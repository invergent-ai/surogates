// An administrator's word on Windows (src/files/admins-own-win32.ts), as the install record must be before the app takes
// it: the file and its folder owned by SYSTEM or the Administrators, with no one else let change either, and neither a
// reparse point. On the runner's own NTFS, each access list set by icacls. It needs dist/native/ntat.node
// (native/ntat/build.ps1) and runs on Windows alone.

import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { adminsOwn } from "../src/files/admins-own-win32.js";
import { installBase } from "../src/vm/image.js";

const WINDOWS = process.platform === "win32";
const SYSTEM = "*S-1-5-18";
const ADMINISTRATORS = "*S-1-5-32-544";
const USERS = "*S-1-5-32-545";

// The runner's own account, which makes every file and folder here.
const me = () => /S-1-[0-9-]+/.exec(execFileSync("whoami", ["/user", "/fo", "csv", "/nh"], { encoding: "utf8" }))![0];

// *path*'s owner and its own access list, exactly *grants*: nothing inherited, and no entry left for the account that
// made it, as Windows gives the maker of a folder on the runner.
function setAcl(path: string, owner: string, grants: string[]): void {
  execFileSync("icacls", [path, "/setowner", owner], { stdio: "pipe" });
  execFileSync("icacls", [path, "/inheritance:r", "/grant:r", ...grants], { stdio: "pipe" });
  execFileSync("icacls", [path, "/remove:g", `*${me()}`], { stdio: "pipe" });
}

const folderGrants = (users = "(OI)(CI)(RX)") => [`${SYSTEM}:(OI)(CI)(F)`, `${ADMINISTRATORS}:(OI)(CI)(F)`, `${USERS}:${users}`];
const fileGrants = (users = "(RX)") => [`${SYSTEM}:(F)`, `${ADMINISTRATORS}:(F)`, `${USERS}:${users}`];

let base: string;
let folder: string;
let record: string;

beforeEach(() => {
  if (!WINDOWS) return;
  // Its long names, as Windows gives them: the runner's temp folder is named by a short name.
  base = realpathSync.native(mkdtempSync(join(tmpdir(), "record-")));
  folder = join(base, "Surogate");
  mkdirSync(folder);
  record = join(folder, "install.json");
  writeFileSync(record, JSON.stringify({ base: "https://surogate.ai", channel: "stable" }));
  setAcl(folder, ADMINISTRATORS, folderGrants());
  setAcl(record, SYSTEM, fileGrants());
});

afterEach(() => {
  if (WINDOWS) rmSync(base, { recursive: true, force: true });
});

describe.runIf(WINDOWS)("an administrator's word on Windows", { timeout: 30_000 }, () => {
  it("takes a record SYSTEM owns, in a folder the Administrators own, that no one else may change", () => {
    expect(() => adminsOwn(record)).not.toThrow();
    expect(installBase(record, true)).toBe("https://surogate.ai");
  });

  it("refuses a record its users may change", () => {
    setAcl(record, SYSTEM, fileGrants("(M)"));
    expect(() => adminsOwn(record)).toThrow(`${record} is not the installer's: only administrators may change it`);
    expect(() => installBase(record, true)).toThrow("only administrators may change it");
  });

  it("refuses a record in a folder its users may put files in", () => {
    setAcl(folder, ADMINISTRATORS, folderGrants("(OI)(CI)(RX,WD,AD)"));
    expect(() => adminsOwn(record)).toThrow(`${folder} is not the installer's: only administrators may change it`);
  });

  it("refuses a record owned by a user, an administrator's own account included", () => {
    setAcl(record, `*${me()}`, fileGrants());
    expect(() => adminsOwn(record)).toThrow(`${record} is not the installer's: only an administrator may own it`);
  });

  it("refuses a record that is a link, though it leads to one it would take", () => {
    const real = join(folder, "real.json");
    writeFileSync(real, JSON.stringify({ base: "https://surogate.ai" }));
    setAcl(real, SYSTEM, fileGrants());
    rmSync(record);
    symlinkSync(real, record, "file");
    expect(() => adminsOwn(record)).toThrow(`${record} is not the installer's: it is a link`);
  });

  it("refuses a folder that is a junction, though it leads to one it would take", () => {
    const junction = join(base, "Linked");
    symlinkSync(folder, junction, "junction");
    expect(() => adminsOwn(join(junction, "install.json"))).toThrow(expect.objectContaining({ code: "ELOOP", refused: "link" }));
  });

  it("says a record not there is no install's, as Node says a file not there is", () => {
    rmSync(record);
    expect(() => adminsOwn(record)).toThrow(expect.objectContaining({ code: "ENOENT" }));
    expect(() => installBase(record, true)).toThrow("Surogate was not installed by its install script");
  });

  it("refuses a path that is not a whole path on a drive", () => {
    for (const path of ["install.json", "Surogate\\install.json", "\\\\?\\C:\\ProgramData\\Surogate\\install.json", "C:\\ProgramData\\..\\x\\install.json"]) {
      expect(() => adminsOwn(path)).toThrow(`${path} is not a whole path on a drive`);
    }
  });
});

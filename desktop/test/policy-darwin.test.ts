// The file helper's sandbox as macOS takes it (hosts/srt-darwin.ts): the policy, the folders no sandbox may be given,
// the tools, the line, the sockets and the longest folder path. Each as the darwin side answers, here on any OS; on a
// Mac, the folder's checks against real folders too.

import { chmodSync, mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { importedOn } from "./platforms.js";

const onMac = () => importedOn("darwin", () => Promise.all([import("../src/hosts/policy.js"), import("../src/hosts/srt-os.js"), import("../src/binding/folder.js")]));

describe("the file helper's sandbox on macOS", () => {
  it("reads the system, the app, the folder and its working folder, writes the last two, reaches no host, and names no bwrap or socat", async () => {
    const [{ sandboxPolicy }] = await onMac();
    expect(sandboxPolicy({ folder: "/f", tmp: "/t", appDirs: ["/app"], reads: ["/copy"], writes: ["/kept"] })).toEqual({
      network: { allowedDomains: [], deniedDomains: [] },
      filesystem: {
        denyRead: ["/"],
        allowRead: [
          "/usr", "/bin", "/sbin", "/System/Library", "/System/Cryptexes", "/System/Volumes/Preboot/Cryptexes", "/Library/Apple",
          "/private/etc", "/private/var/db/timezone", "/dev", "/app", "/f", "/t", "/copy", "/kept",
        ],
        allowWrite: ["/f", "/t", "/kept"],
        denyWrite: ["/tmp/claude", "/private/tmp/claude"],
      },
    });
  });

  it("never reads /System whole, whose data volume holds every user's home folder", async () => {
    const [, { srtOs }] = await onMac();
    expect(srtOs.system).not.toContain("/System");
    expect(srtOs.system.filter((dir) => "/System/Volumes/Data/Users/x".startsWith(`${dir}/`))).toEqual([]);
  });

  it.each([
    "/System", "/System/Library", "/Library", "/Library/LaunchDaemons", "/private", "/private/etc", "/private/var/db", "/private/var/root",
    "/dev", "/dev/fd", "/Volumes", "/cores", "/tmp/claude", "/private/tmp/claude/work",
  ])("refuses %s", async (path) => {
    const [{ isReserved }] = await onMac();
    expect(isReserved(path)).toBe(true);
  });

  it.each([
    "/Users/flavius/Documents/work", "/Volumes/USB", "/Volumes/USB/work", "/private/tmp/work", "/private/var/folders/xy/abc/T/work",
    "/Users/Shared/work", "/Applicationsx", "/Libraryx",
  ])("accepts %s, as Linux takes a folder in /tmp or on a drive", async (path) => {
    const [{ isReserved }] = await onMac();
    expect(isReserved(path)).toBe(false);
  });

  it("lacks ripgrep alone, on the PATH that begins with the folder of the node it runs on, and says so in a Mac's words", async () => {
    const [{ fileToolsMissing, toolsMissing }, { srtOs }] = await onMac();
    const bin = mkdtempSync(join(tmpdir(), "file-tools-"));
    try {
      expect(fileToolsMissing("/no/bwrap", bin)).toEqual(["ripgrep"]);
      writeFileSync(join(bin, "rg"), "#!/bin/sh\n");
      chmodSync(join(bin, "rg"), 0o755);
      expect(fileToolsMissing(undefined, bin)).toEqual([]);
      expect(srtOs.toolsPath("/usr/bin:/bin")).toBe(`${dirname(process.execPath)}:/usr/bin:/bin`);
      expect(toolsMissing(["ripgrep"])).toBe("Surogate's sandbox tools are missing. Install Surogate again. It lacks ripgrep");
    } finally {
      rmSync(bin, { recursive: true, force: true });
    }
  });

  it("lets the helper map fsat as code, by a rule at the end of srt's profile, and refuses a line it does not recognise", async () => {
    const { mapFsat } = await importedOn("darwin", () => import("../src/hosts/srt-darwin.js"));
    const line = `env TMPDIR=/tmp/claude /usr/bin/sandbox-exec -p '(version 1)\n(deny default)\n(allow file-read* (subpath "/f/it'"'"'s"))' /bin/bash -c 'node helper.js'`;
    expect(mapFsat(line, "/app/dist/native/fsat.node")).toBe(
      `env TMPDIR=/tmp/claude /usr/bin/sandbox-exec -p '(version 1)\n(deny default)\n(allow file-read* (subpath "/f/it'"'"'s"))\n(allow file-map-executable (literal "/app/dist/native/fsat.node"))' /bin/bash -c 'node helper.js'`,
    );
    expect(() => mapFsat("/bin/bash -c 'node helper.js'")).toThrow(/cannot let the file helper load fsat/);
    expect(() => mapFsat("/usr/bin/sandbox-exec -p '(version 1)")).toThrow(/cannot let the file helper load fsat/);
    expect(() => mapFsat(line, "/it's/fsat.node")).toThrow(/holds a quote/);
  });

  it("puts its sockets where a Mac's 104 bytes take them, and leaves no process of srt's", async () => {
    const [, { srtOs }] = await onMac();
    // srt-mux-<5 digits>-<4 base-36 digits>.sock is 23 bytes, behind a slash, after the folder's own slash.
    expect(srtOs.socketsFit(`/${"d".repeat(103 - 25)}`)).toBe(true);
    expect(srtOs.socketsFit(`/${"d".repeat(103 - 24)}`)).toBe(false);
    const dir = mkdtempSync(join(tmpdir(), "srt-sweep-"));
    writeFileSync(join(dir, "left"), "");
    srtOs.sweep(dir);
    expect(() => realpathSync(dir)).toThrow();
  });
});

describe("a chat's folder on macOS", () => {
  let base: string;
  beforeEach(() => {
    base = realpathSync(mkdtempSync(join(tmpdir(), "folder-darwin-")));
  });
  afterEach(() => rmSync(base, { recursive: true, force: true }));

  // A folder whose real path is *bytes* long, under base.
  const long = (bytes: number): string => {
    let path = base;
    while (Buffer.byteLength(path) + 200 < bytes) path = join(path, "d".repeat(199));
    path = join(path, "e".repeat(bytes - Buffer.byteLength(path) - 1));
    mkdirSync(path, { recursive: true });
    return path;
  };
  const guards = () => ({ home: join(base, "home"), dataDir: join(base, "data"), cacheDir: join(base, "cache"), appDirs: [join(base, "app")] });

  it("is refused at its binding where its path is longer than the sandbox's profile takes, and taken at 1 000 bytes", async () => {
    const [, , { checkFolder }] = await onMac();
    const most = long(1000);
    expect(checkFolder(most, guards())).toMatchObject({ ok: true, path: most });
    const over = long(1001);
    expect(checkFolder(over, guards())).toEqual({
      ok: false, missing: false, message: `this computer cannot sandbox a folder whose path is longer than 1000 bytes: ${over}`,
    });
  });
});

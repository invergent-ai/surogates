// The file host's open flags that are not the same on every OS: each OS's row, as files/flags.ts gives it.

import { constants } from "node:fs";

import { describe, expect, it } from "vitest";

import { importedOn } from "./platforms.js";

describe("the file host's open flags", () => {
  it("are each OS's row: no link followed at a path's last name on Linux, none on Windows, whose walk follows none, and none on an OS the file host is not built for yet", async () => {
    const { FLAGS: linux } = await importedOn("linux", () => import("../src/files/flags.js"));
    expect([linux.NOFOLLOW, linux.DIRECTORY]).toEqual([constants.O_NOFOLLOW, constants.O_DIRECTORY]);
    const { FLAGS: windows, WIN32_FLAGS } = await importedOn("win32", () => import("../src/files/flags.js"));
    // The C runtime's _O_OBTAIN_DIR, which no flag Node takes on Windows has (O_RDONLY 0 to O_EXCL 0x400, UV_FS_O_FILEMAP 0x20000000).
    expect([windows.NOFOLLOW, windows.DIRECTORY, WIN32_FLAGS.O_DIRECTORY]).toEqual([0, 0x2000, 0x2000]);
    const { FLAGS: other } = await importedOn("freebsd", () => import("../src/files/flags.js"));
    expect(() => other.NOFOLLOW).toThrow(new Error("The file host is not built for freebsd yet"));
  });
});

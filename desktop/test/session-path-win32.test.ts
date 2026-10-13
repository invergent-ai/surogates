// A folder's path in a session on Windows (session-path.ts): the server, the model, the guest and the file host's checks
// name C:\Users\Ana\Documents\shop as /mnt/c/Users/Ana/Documents/shop, the form WSL gives the same folder; only where
// the folder is opened on this computer is it named by its own path again. A folder that is on no drive letter, as a
// network share is, has no path in the guest.

import { describe, expect, it } from "vitest";

import { importedOn } from "./platforms.js";

const windows = () => importedOn("win32", () => import("../src/session-path.js"));

describe("a folder's path in the guest on Windows", () => {
  it("is /mnt/, the drive's letter in small, and the folder's names as the folder has them", async () => {
    const { guestPathOf } = await windows();
    expect(guestPathOf("C:\\Users\\Ana\\Documents\\shop")).toBe("/mnt/c/Users/Ana/Documents/shop");
    expect(guestPathOf("D:\\Projects\\Shop Front")).toBe("/mnt/d/Projects/Shop Front");
    expect(guestPathOf("c:\\Users\\Ana")).toBe("/mnt/c/Users/Ana");
    // Windows takes either slash, and a trailing one names the same folder.
    expect(guestPathOf("C:/Users/Ana/Documents/")).toBe("/mnt/c/Users/Ana/Documents");
    expect(guestPathOf("E:\\")).toBe("/mnt/e");
    // Names a POSIX path holds as they are, whatever Windows makes of them elsewhere.
    expect(guestPathOf("C:\\Users\\Ana\\Ünïcødé ✓\\a#b%c$d")).toBe("/mnt/c/Users/Ana/Ünïcødé ✓/a#b%c$d");
  });

  it("is the folder's own path again on this computer, the drive's letter large", async () => {
    const { guestPathOf, hostPathOf } = await windows();
    expect(hostPathOf("/mnt/c/Users/Ana/Documents/shop")).toBe("C:\\Users\\Ana\\Documents\\shop");
    expect(hostPathOf("/mnt/e")).toBe("E:\\");
    for (const path of ["C:\\Users\\Ana\\Documents\\shop", "D:\\Projects\\Shop Front", "E:\\", "C:\\Users\\Ana\\Ünïcødé ✓"]) {
      expect(hostPathOf(guestPathOf(path)), path).toBe(path);
    }
    for (const path of ["/mnt/c/Users/Ana", "/mnt/z/a b/c"]) expect(guestPathOf(hostPathOf(path)), path).toBe(path);
  });

  it("is none for a folder on no drive of this computer's: a network share, a device's namespace, a relative path", async () => {
    const { guestPathOf } = await windows();
    for (const path of [
      "\\\\server\\share\\shop", "//server/share/shop", "\\\\?\\C:\\Users\\Ana", "\\\\?\\UNC\\server\\share", "\\\\.\\C:\\Users",
      "\\Users\\Ana", "C:Users\\Ana", "Users\\Ana", "", "C:",
    ]) expect(() => guestPathOf(path), path).toThrow(/is not a folder on a drive of this computer/);
  });

  it("is none for a path Windows would read as another folder: . and .., a name that ends in a dot or a space, or one a name cannot hold", async () => {
    const { guestPathOf, hostPathOf } = await windows();
    for (const path of ["C:\\Users\\..\\Windows", "C:\\Users\\.\\Ana", "C:\\Users\\Ana.\\x", "C:\\Users\\Ana \\x", "C:\\Users\\A\0na"]) {
      expect(() => guestPathOf(path), JSON.stringify(path)).toThrow(/cannot be named in the guest/);
    }
    for (const path of [
      "/mnt/c/Users/../Windows", "/mnt/c/Users/./Ana", "/mnt/c/Users/Ana./x", "/mnt/c/Users/Ana /x", "/mnt/c/a\\b", "/mnt/c/a:b", "/mnt/c/a*b",
      "/mnt/c/a?b", "/mnt/c/a\"b", "/mnt/c/a<b", "/mnt/c/a>b", "/mnt/c/a|b", "/mnt/c/a\x01b", "/mnt/c/a\0b",
    ]) expect(() => hostPathOf(path), JSON.stringify(path)).toThrow(/names no path on this computer/);
  });

  it("is none for a guest path outside every drive's folder", async () => {
    const { hostPathOf } = await windows();
    for (const path of ["/home/ana", "/mnt", "/mnt/", "/mnt/cd/x", "/mnt/C/x", "/mnt/1/x", "mnt/c/x", "/MNT/c/x", "", "/"]) {
      expect(() => hostPathOf(path), path).toThrow(/names no path on this computer/);
    }
  });
});

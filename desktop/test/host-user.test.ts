// Whom the guest's roots run for, this computer's user, by its OS (host-user.ts): on Linux and macOS, the user's own
// uid, gid, name and home, as the app has always given them; on Windows, which has no uid, Linux's first user's, the
// user's name made one a Linux user can have, and the home's path in the guest.

import { homedir, userInfo } from "node:os";

import { describe, expect, it, vi } from "vitest";

import { importedOn } from "./platforms.js";

it("is this computer's user as the OS names them on Linux and macOS", async () => {
  const { uid, gid, username } = userInfo();
  for (const os of ["linux", "darwin"] as const) {
    const { hostUser } = await importedOn(os, () => import("../src/host-user.js"));
    expect(hostUser(), os).toEqual({ uid, gid, name: username, home: homedir() });
  }
});

describe("this computer's user on Windows", () => {
  // The user Windows names, and the profile folder it gives them, as Node reads them there.
  async function windowsUser(username: string, home: string) {
    vi.doMock("node:os", async (actual) => ({
      ...(await actual<typeof import("node:os")>()),
      userInfo: () => ({ uid: -1, gid: -1, username, homedir: home, shell: null }),
      homedir: () => home,
    }));
    try {
      return (await importedOn("win32", () => import("../src/host-user.js"))).hostUser();
    } finally {
      vi.doUnmock("node:os");
    }
  }

  it("is uid and gid 1000, the user's name in small letters, and the profile's path in the guest", async () => {
    expect(await windowsUser("Ana", "C:\\Users\\Ana")).toEqual({ uid: 1000, gid: 1000, name: "ana", home: "/mnt/c/Users/Ana" });
    expect(await windowsUser("ana.lopez", "D:\\Profiles\\ana.lopez")).toEqual({ uid: 1000, gid: 1000, name: "ana.lopez", home: "/mnt/d/Profiles/ana.lopez" });
  });

  it("has each character a Linux user name cannot hold in its name made _, and is user where none of its own is left", async () => {
    expect((await windowsUser("Ana María", "C:\\Users\\Ana María")).name).toBe("ana_mar_a");
    expect((await windowsUser("-Ana$", "C:\\Users\\-Ana$")).name).toBe("_ana_");
    expect((await windowsUser("José:x", "C:\\Users\\José")).name).toBe("jos__x");
    expect((await windowsUser("Владимир", "C:\\Users\\Владимир")).name).toBe("user");
    expect((await windowsUser("", "C:\\Users\\x")).name).toBe("user");
  });
});

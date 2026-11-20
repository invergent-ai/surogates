// Whom the guest's roots run for, this computer's user, by its OS (host-user.ts): on Linux and macOS, the user's own
// uid, gid and name; on Windows, which has no uid, Linux's first user's, and the user's name made one a Linux user
// can have. The home is the root's private one at a fixed path on every OS.

import { userInfo } from "node:os";

import { describe, expect, it, vi } from "vitest";

import { GUEST_HOME } from "../src/host-user.js";
import { importedOn } from "./platforms.js";

it("is this computer's user as the OS names them on Linux and macOS", async () => {
  const { uid, gid, username } = userInfo();
  for (const os of ["linux", "darwin"] as const) {
    const { hostUser } = await importedOn(os, () => import("../src/host-user.js"));
    expect(hostUser(), os).toEqual({ uid, gid, name: username, home: GUEST_HOME });
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

  it("is uid and gid 1000, the user's name in small letters, and the private home", async () => {
    expect(await windowsUser("Ana", "C:\\Users\\Ana")).toEqual({ uid: 1000, gid: 1000, name: "ana", home: GUEST_HOME });
    expect(await windowsUser("ana.lopez", "D:\\Profiles\\ana.lopez")).toEqual({ uid: 1000, gid: 1000, name: "ana.lopez", home: GUEST_HOME });
  });

  it("has each character a Linux user name cannot hold in its name made _, and is user where none of its own is left", async () => {
    expect((await windowsUser("Ana María", "C:\\Users\\Ana María")).name).toBe("ana_mar_a");
    expect((await windowsUser("-Ana$", "C:\\Users\\-Ana$")).name).toBe("_ana_");
    expect((await windowsUser("José:x", "C:\\Users\\José")).name).toBe("jos__x");
    expect((await windowsUser("Владимир", "C:\\Users\\Владимир")).name).toBe("user");
    expect((await windowsUser("", "C:\\Users\\x")).name).toBe("user");
  });
});

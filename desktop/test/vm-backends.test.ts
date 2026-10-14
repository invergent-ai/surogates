// The VM's backend for each OS, in one table: what boots its VM, what of this computer the VM
// needs, where its sockets go, and how long a socket's path may be there. Linux's is the backend
// the app has always had; an OS whose backend is not built yet has none.

import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, expect, it } from "vitest";

import { backendFor } from "../src/vm/backends.js";
import { bootDarwin } from "../src/vm/darwin.js";
import { DOOR } from "../src/vm/inbound.js";
import { bootLinux, missingTools } from "../src/vm/linux.js";
import { addressesWin32, bootWin32, requirementsWin32, runtimeFolderWin32 } from "../src/vm/win32.js";

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "vm-backends-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

it("is Linux's on Linux, macOS's on macOS (vm-darwin-boot.test.ts), Windows's on Windows (vm-win32.test.ts), and none yet elsewhere", () => {
  expect(backendFor("linux")?.boot).toBe(bootLinux);
  expect(backendFor("darwin")?.boot).toBe(bootDarwin);
  expect(backendFor("win32")?.boot).toBe(bootWin32);
  expect(backendFor("freebsd")).toBeNull();
});

it("on Windows, reaches the guest's ports and door as named pipes, with no socket-path limit, and says what the computer lacks", async () => {
  const win32 = backendFor("win32")!;
  const ana = { uid: 1000, gid: 1000, name: "ana", home: "/mnt/c/Users/ana" };
  const data = "C:\\Users\\ana\\AppData\\Local\\Surogate";
  expect(win32.socketMax).toBeNull();
  expect(win32.runtimeFolder(ana, { LOCALAPPDATA: "C:\\Users\\ana\\AppData\\Local" }, data)).toBe(runtimeFolderWin32({ LOCALAPPDATA: "C:\\Users\\ana\\AppData\\Local" }, data));
  const run = win32.runtimeFolder(ana, { LOCALAPPDATA: "C:\\Users\\ana\\AppData\\Local" }, data);
  expect(win32.addresses(run, ana)).toEqual(addressesWin32(run));
  // The requirements come from surogate-vm check; here from its facts, with no backend wired to a program.
  expect(await requirementsWin32(async () => ({ virtualMachinePlatform: true, service: "present", hostCompute: "ok" }))).toEqual([]);
});

it("on Linux, needs what the VM has always needed of this computer, looked for by the same rule", async () => {
  const linux = backendFor("linux")!;
  const bin = join(dir, "bin");
  mkdirSync(bin);
  for (const [name, script] of [["qemu-system-x86_64", "echo 'QEMU emulator version 10.1.0'"], ["newuidmap", "true"], ["newgidmap", "true"]] as const) {
    writeFileSync(join(bin, name), `#!/bin/sh\n${script}\n`);
    chmodSync(join(bin, name), 0o755);
  }
  const none = join(dir, "none");
  mkdirSync(none);
  // Its tools on the PATH looked at; then the same PATH, the tools' folder one a chat is bound to, where none is looked for.
  const asks = [{ unpacking: true, held: [], path: bin }, { unpacking: false, held: [statSync(bin)], path: `${bin}:${none}` }, { unpacking: true, held: [], path: none }];
  for (const ask of asks) expect(await linux.requirements(ask), JSON.stringify(ask)).toEqual(await missingTools({}, ask.unpacking, ask.held, ask.path));
  expect(await linux.requirements(asks[1]!)).toEqual(expect.arrayContaining(["QEMU 8.2 or later", "newuidmap and newgidmap"]));
  expect(await linux.requirements(asks[0]!)).not.toContain("QEMU 8.2 or later");
});

it("on Linux, keeps the VM's sockets in this user's runtime folder, one for each data folder, a path of 107 bytes at most", () => {
  const linux = backendFor("linux")!;
  const ana = { uid: 1234, gid: 1234, name: "ana", home: "/home/ana" };
  const data = "/home/ana/.local/share/surogate";
  const own = `vm-${createHash("sha256").update(data).digest("hex").slice(0, 8)}`;
  expect(linux.runtimeFolder(ana, { XDG_RUNTIME_DIR: "/run/user/1000" }, data)).toBe(`/run/user/1000/surogate/${own}`);
  // Without XDG_RUNTIME_DIR, or with it empty: the folder logind makes for the user.
  expect(linux.runtimeFolder(ana, {}, data)).toBe(`/run/user/1234/surogate/${own}`);
  expect(linux.runtimeFolder(ana, { XDG_RUNTIME_DIR: "" }, data)).toBe(`/run/user/1234/surogate/${own}`);
  expect(linux.socketMax).toBe(107);
});

it("on Linux and macOS, reaches the guest's three ports and the browser's door as Unix sockets in the runtime folder, by the names they have always had", () => {
  const ana = { uid: 1234, gid: 1234, name: "ana", home: "/home/ana" };
  for (const os of ["linux", "darwin"] as const) {
    const run = os === "linux" ? "/run/user/1234/surogate/vm-1a2b3c4d" : "/private/var/folders/ab/cdef/T/surogate/vm-1a2b3c4d";
    expect(backendFor(os)!.addresses(run, ana), os).toEqual({
      control: join(run, "control.sock"), net: join(run, "net.sock"), inbound: join(run, "inbound.sock"), door: join(run, DOOR),
    });
  }
  expect(DOOR).toBe("browser.sock");
});

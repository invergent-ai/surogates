// The macOS VM backend's parts that start no program (src/vm/darwin.ts): its row of the backend table,
// its runtime folder and the bound on its sockets' paths, and the guest kernel's command line. They
// pass on a Mac as on Linux (vitest.config.ts, MACOS); its boot is vm-darwin-boot.test.ts's.

import { existsSync, mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { backendFor } from "../src/vm/backends.js";
import { bootDarwin, commandLine, fitting, runtimeFolder } from "../src/vm/darwin.js";
import type { VmOptions } from "../src/vm/manager.js";
import { KERNEL_COMMAND_LINE } from "../src/vm/qemu.js";

const ANA = { uid: 501, gid: 20, name: "ana", home: "/Users/ana" };

let dir: string;

beforeEach(() => {
  dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-darwin-")));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

const boot = (extra: Partial<VmOptions>) => bootDarwin({
  kernel: "/i/vmlinuz", rootfs: "/i/rootfs.img", agentDisk: "/a/agent.img", sessions: join(dir, "data", "vm", "sessions.img"),
  console: join(dir, "data", "logs", "vm-console.log"), run: join(dir, "run"), user: ANA, program: join(dir, "surogate-vm"), ...extra,
}, undefined, performance.now() + 5_000);

describe("the macOS VM backend", () => {
  it("is macOS's row of the backend table: its boot, its runtime folder under the user's temp folder, and Darwin's socket bound", () => {
    const darwin = backendFor("darwin")!;
    expect(darwin.boot).toBe(bootDarwin);
    expect(darwin.socketMax).toBe(103);
    expect(darwin.runtimeFolder(ANA, { TMPDIR: "/var/folders/ab/cdef/T/" }, "/Users/ana/Library/Application Support/Surogate"))
      .toBe(runtimeFolder("/Users/ana/Library/Application Support/Surogate", "/var/folders/ab/cdef/T"));
    expect(runtimeFolder("/Users/ana/Library/Application Support/Surogate", "/var/folders/ab/cdef/T")).toMatch(/^\/var\/folders\/ab\/cdef\/T\/surogate\/vm-[0-9a-f]{8}$/);
  });

  it("refuses a runtime folder whose sockets' paths are longer than Darwin takes, saying so, and fits one under a temp folder of 60 bytes", async () => {
    const sixty = `/var/folders/${"t".repeat(60 - "/var/folders/".length)}`;
    expect(Buffer.byteLength(join(runtimeFolder(join(dir, "data"), sixty), "inbound.sock"))).toBeLessThanOrEqual(103);
    const run = runtimeFolder(join(dir, "data"), join(dir, "t".repeat(90)));
    await expect(boot({ run })).rejects.toThrow(/^this Mac's temp folder's path is too long for the sandbox's sockets: .*\/surogate\/vm-[0-9a-f]{8}\/\w+\.sock is \d+ bytes, over 103$/);
    expect(existsSync(run)).toBe(false);
  });

  it("names the runtime folder by the temp folder's real path, as surogate-vm takes paths with no link on their way", () => {
    const real = join(dir, "real");
    mkdirSync(real);
    symlinkSync(real, join(dir, "var"));
    expect(runtimeFolder("/Users/ana/Library/Application Support/Surogate", join(dir, "var"))).toBe(runtimeFolder("/Users/ana/Library/Application Support/Surogate", real));
    expect(runtimeFolder("/data", "/no/such/temp")).toMatch(/^\/no\/such\/temp\/surogate\/vm-[0-9a-f]{8}$/);
  });

  it("tells the guest kernel the uid every root runs as, which the protected-names rule judges, on every OS's command line", () => {
    expect(commandLine(ANA)).toBe(`${KERNEL_COMMAND_LINE} surogate.uid=501`);
    expect(fitting(runtimeFolder("/Users/ana/Library/Application Support/Surogate", "/var/folders/ab/cdefghij/T"))).toBeNull();
  });
});

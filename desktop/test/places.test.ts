import { describe, expect, it } from "vitest";

import { readonlyFlag } from "../src/vm/linux.js";
import { virtiofsdArgs } from "../src/vm/qemu.js";

describe("a folder's virtiofsd for the agent's own git", () => {
  it("maps the host user to the guest's root, and refuses every write where the folder is shared read-only", () => {
    expect(virtiofsdArgs("/home/ana/Documents", "/run/vm/vfs-2.sock", 0, { uid: 1000, gid: 1001 }, true)).toEqual([
      "--shared-dir=/home/ana/Documents", "--socket-path=/run/vm/vfs-2.sock", "--sandbox=namespace", "--cache=never",
      "--uid-map=:0:1000:1:", "--gid-map=:0:1001:1:", "--readonly",
    ]);
    expect(virtiofsdArgs("/d/history/k", "/run/vm/vfs-1.sock", 0, { uid: 1000, gid: 1001 })).not.toContain("--readonly");
  });

  it("is given --readonly only by a virtiofsd that has it", () => {
    // Ubuntu 26.04's 1.13.2 lists it; 24.04's 1.10.0 does not.
    expect(readonlyFlag("      --sandbox <SANDBOX>\n      --readonly\n          Prevent the guest from making modifications\n")).toBe(true);
    expect(readonlyFlag("      --sandbox <SANDBOX>\n      --seccomp <SECCOMP>\n      --no-readonly-thing\n")).toBe(false);
  });
});

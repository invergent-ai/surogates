// For the tests alone: with SUROGATE_VM_STANDIN=hyperv, every boot of the VM suite's on Linux is the stand-in for
// Hyper-V's devices (standin.ts), so the suite runs the guest's Windows path. Loaded by vitest.config.ts only then.

import { vi } from "vitest";

if (process.env.SUROGATE_VM_STANDIN !== "hyperv") throw new Error(`SUROGATE_VM_STANDIN=${process.env.SUROGATE_VM_STANDIN} names no stand-in: hyperv is the one`);

vi.mock("../../../src/vm/linux.js", async (original) => {
  const linux = await original<typeof import("../../../src/vm/linux.js")>();
  const { bootStandin } = await import("./standin.js");
  return { ...linux, bootLinux: bootStandin };
});

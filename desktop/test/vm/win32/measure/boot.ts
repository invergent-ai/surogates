// How long the x64 guest takes from surogate-vm.exe's start to the agent's hello on this PC, booted directly by HCS's
// LinuxKernelDirect with the image's bzImage and Hyper-V's initrd, three SCSI disks, the console on COM1, and the
// protected-names rule attached. Median of three; the target is under 1.5 s (Linux, under KVM: 650 to 752 ms).
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts boot
// PASS or FAIL: the image boots to hello under 1.5 s at the median, and the rule attached at every boot.

import { readFileSync } from "node:fs";

import { expect, it } from "vitest";

import { lab, median, ms, record, ROOTS } from "./harness.js";

it("boots the x64 guest to its hello through HCS, with its initrd", async () => {
  const times: number[] = [];
  const attached: boolean[] = [];
  let console_ = "";
  for (let n = 0; n < 3; n += 1) {
    const begun = performance.now();
    const booted = await lab(`boot-${n}`);
    times.push(ms(begun));
    console_ = readFileSync(booted.options.console, "utf8");
    attached.push(console_.includes("surogate: the protected-names rule attached 16 of 16 hooks"));
    await booted.run(ROOTS[0]!, "true").catch(() => undefined);
    await booted.stop();
  }
  const failed = record("boot", {
    "boot to hello, ms": times, "median, ms": median(times), "the last console, its tail": console_.trim().split("\n").slice(-20).join("\n"),
  }, {
    "the image boots to hello in under 1.5 s at the median": median(times) < 1_500,
    "the protected-names rule attached at every boot": attached.every(Boolean),
  });
  expect(failed).toEqual([]);
});

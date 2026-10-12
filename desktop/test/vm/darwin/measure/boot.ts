// How long the arm64 guest takes from surogate-vm's start to the agent's hello on this Mac, with the
// image's kernel (the uncompressed arm64 Image) and with that kernel gzipped, which VZLinuxBootLoader may
// or may not take (Apple: "The kernel and RAM disk image must support the CPU architecture of your Mac");
// that the protected-names rule attached at each boot; and surogate-vm's own check, as native/build.sh
// signs it and with its signature taken off. Decides the kernel's form the image keeps.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts boot
// Prints each boot's time, and PASS or FAIL: the image's kernel boots, under 1.5 s at the median of
// three (Linux, under KVM: 650 to 752 ms), the rule attached, and the signed surogate-vm says supported.

import { execFileSync } from "node:child_process";
import { constants, copyFileSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

import { expect, it } from "vitest";

import { IMAGE, lab, median, ms, PROGRAM, record, scratch } from "./harness.js";

const check = (program: string) => {
  try {
    return execFileSync(program, ["check"], { encoding: "utf8" }).trim();
  } catch (error) {
    return `it did not answer: ${(error as Error).message}`;
  }
};

it("boots the arm64 guest to its hello, with each form of its kernel", async () => {
  const times: number[] = [];
  const attached: boolean[] = [];
  for (let n = 0; n < 3; n += 1) {
    const begun = performance.now();
    const booted = await lab(`boot-${n}`);
    times.push(ms(begun));
    attached.push(readFileSync(booted.options.console, "utf8").includes("surogate: the protected-names rule attached 15 of 15 hooks"));
    await booted.stop();
  }
  // The same kernel gzipped, beside a clone of the image's disk: surogate-vm takes both from one folder.
  const gzipped = scratch("boot-gzip");
  writeFileSync(join(gzipped, "vmlinuz"), gzipSync(readFileSync(join(IMAGE, "vmlinuz"))));
  copyFileSync(join(IMAGE, "rootfs.img"), join(gzipped, "rootfs.img"), constants.COPYFILE_FICLONE);
  let gzip: string;
  try {
    const begun = performance.now();
    const booted = await lab("boot-gzip", { kernel: join(gzipped, "vmlinuz"), rootfs: join(gzipped, "rootfs.img") });
    gzip = `booted in ${ms(begun)} ms`;
    await booted.stop();
  } catch (error) {
    gzip = `did not boot: ${(error as Error).message}`;
  }
  const unsigned = join(scratch("boot-unsigned"), "surogate-vm");
  copyFileSync(PROGRAM, unsigned);
  execFileSync("/usr/bin/codesign", ["--remove-signature", unsigned]);
  const signed = check(PROGRAM);
  const failed = record("boot", {
    "boot to hello, ms, the image's kernel": times, "median, ms": median(times), "the kernel gzipped": gzip,
    "surogate-vm check, signed": signed, "surogate-vm check, its signature taken off": check(unsigned),
  }, {
    "the image's kernel boots to hello in under 1.5 s at the median": median(times) < 1_500,
    "the protected-names rule attached at every boot": attached.every(Boolean),
    "the signed surogate-vm says this Mac can run the guest": JSON.parse(signed).supported === true,
  });
  expect(failed).toEqual([]);
});

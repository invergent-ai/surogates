// The host memory a 2 GiB guest holds over time, without the free-page reporting QEMU gives on Linux (Apple's
// balloon, VZVirtioTraditionalMemoryBalloonDevice, is never inflated): the framework's VM process's resident
// size idle, at a command's 1.5 GiB peak, and each minute after it ends, for MEASURE_MINUTES (20 unless
// set). Decides a ceiling to write down, or the balloon's use.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts memory
// Written down, no PASS or FAIL: the resident sizes, in MiB.

import { execFileSync } from "node:child_process";

import { expect, it } from "vitest";

import { lab, ready, record, ROOTS, scratch } from "./harness.js";

const vms = () => {
  try {
    return execFileSync("/usr/bin/pgrep", ["-f", "com.apple.Virtualization.VirtualMachine"], { encoding: "utf8" }).trim().split("\n").filter(Boolean).map(Number);
  } catch {
    return [];
  }
};
const resident = (pids: number[]) => pids.reduce((sum, pid) => {
  try {
    return sum + Number(execFileSync("/bin/ps", ["-o", "rss=", "-p", String(pid)], { encoding: "utf8" }).trim()) / 1024;
  } catch {
    return sum;
  }
}, 0);

it("holds the guest's memory over time", async () => {
  const before = vms();
  const booted = await lab("memory");
  const ours = vms().filter((pid) => !before.includes(pid));
  const root = ROOTS[0]!;
  await ready(booted.guest, root, scratch("memory"));
  const idle = resident(ours);
  const peak = booted.run(root, "python3 -c 'import time; a = bytearray(1536 * 1024 * 1024); a[::4096] = b\"x\" * len(a[::4096]); time.sleep(20)'", 300);
  await new Promise((resolve) => setTimeout(resolve, 15_000));
  const atPeak = resident(ours);
  await peak;
  const after: number[] = [];
  for (let minute = 0; minute < Number(process.env.MEASURE_MINUTES ?? 20); minute += 1) {
    await new Promise((resolve) => setTimeout(resolve, 60_000));
    after.push(Math.round(resident(ours)));
  }
  await booted.stop();
  record("memory", { "the framework's VM processes": ours, "idle, MiB": Math.round(idle), "at the peak, MiB": Math.round(atPeak), "each minute after, MiB": after });
  expect(ours.length).toBeGreaterThan(0);
});

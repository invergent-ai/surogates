// How long the arm64 guest takes from surogate-vm's start to the agent's hello on this Mac, and where that time
// goes. The first boot makes the sessions disk (vm/init's mke2fs), as only the app's first boot does; the three
// after it find the disk there and check it, as every later boot does, and their median is the target's. One boot
// more, with the measurements' agent disk, has its init write the kernel's own log to the console, each line with
// its time since the kernel's start, and the guest's uptime as the agent starts and as the rule's outcome lands
// (probe.sh). Every boot's timeline (harness.ts) has surogate-vm's start, its lines, the boot line, surogate-vm's
// own times for the configuration validated and the VM running, the console's first byte and each of its lines, and
// the hello. Also: the image's kernel gzipped, which VZLinuxBootLoader refused on a Mac ("Internal Virtualization
// error"; Apple: "The kernel and RAM disk image must support the CPU architecture of your Mac"), so the image keeps
// the uncompressed Image alone (images/guest/kernel-image.sh); that the protected-names rule attached at each boot;
// and surogate-vm's own check, as native/build.sh signs it and signed again without its entitlement.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts boot
// Prints each boot's time and where the kernel's boot went, and PASS or FAIL: the later boots reach hello under
// 1.5 s at the median of three (Linux, under KVM: 650 to 752 ms), the rule attached at every boot, the gzipped
// kernel refused, and surogate-vm says this Mac can run the guest signed, and cannot without its entitlement.

import { execFileSync } from "node:child_process";
import { constants, copyFileSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

import { expect, it } from "vitest";

import type { VmOptions } from "../../../../src/vm/manager.js";
import { DESKTOP, IMAGE, lab, median, ms, PROBING_DISK, PROGRAM, record, scratch, type Timeline, told } from "./harness.js";

// The rule attached whole, by its own list of hooks: its lsm programs (vm/rule.bpf.c), which vm/init expects every one of.
const HOOKS = (readFileSync(join(DESKTOP, "vm", "rule.bpf.c"), "utf8").match(/^SEC\("lsm\//gm) ?? []).length;
const RULE = `surogate: the protected-names rule attached ${HOOKS} of ${HOOKS} hooks`;

const check = (program: string) => {
  try {
    return execFileSync(program, ["check"], { encoding: "utf8" }).trim();
  } catch (error) {
    return `it did not answer: ${(error as Error).message}`;
  }
};

// A boot of the data folder "boot", new for the first, and the time to its hello.
async function boot(extra: Partial<VmOptions>, fresh: boolean): Promise<{ ms: number; attached: boolean; timeline: Timeline }> {
  const begun = performance.now();
  const booted = await lab("boot", extra, undefined, { fresh });
  const took = ms(begun);
  const attached = readFileSync(booted.options.console, "utf8").includes(RULE);
  await booted.stop();
  return { ms: took, attached, timeline: booted.timeline };
}

// The time of the first event of *timeline* whose words start with *what*.
const when = (timeline: Timeline, what: string) => timeline.events.find((event) => event.what.startsWith(what))?.ms;

// Where the kernel's boot went, from the console of the boot whose init wrote the kernel's log (probe.sh): each
// milestone at its time since the kernel's start, and where the kernel's start was on this Mac's timeline.
function kernelTimes(timeline: Timeline): Record<string, number | string> {
  const printk = (pattern: RegExp) => {
    for (const { line } of timeline.console) {
      const at = /^\[\s*(\d+\.\d+)\]\s(.*)$/.exec(line);
      if (at && pattern.test(at[2] ?? "")) return Math.round(Number(at[1]) * 1000);
    }
    return undefined;
  };
  const uptime = (what: string) => {
    for (const { ms: seen, line } of timeline.console) {
      const at = new RegExp(`^surogate-measure: uptime (\\d+\\.\\d+) s, ${what}`).exec(line);
      if (at) return { guest: Math.round(Number(at[1]) * 1000), host: seen };
    }
    return undefined;
  };
  const agent = uptime("the agent's start");
  const rule = uptime("the rule's outcome");
  // The kernel's start on this Mac's timeline: when the agent's start was seen, less the guest's uptime then.
  const start = agent ? agent.host - agent.guest : undefined;
  const sent = when(timeline, "the boot line sent");
  const running = when(timeline, "surogate-vm had the VM running");
  const hello = when(timeline, "the agent's hello");
  const found: Record<string, number | string | undefined> = {
    "the kernel's start, ms on this Mac's timeline (the agent's start seen, less the guest's uptime then)": start,
    "from surogate-vm's VM running to the kernel's start, ms": start !== undefined && running !== undefined ? Math.round(start - running) : undefined,
    "from the boot line to the kernel's start, ms": start !== undefined && sent !== undefined ? Math.round(start - sent) : undefined,
    "the kernel's random pool ready (crng init done), ms after its start": printk(/^random: crng init done/),
    "the image's disk mounted, ms after its start": printk(/^EXT4-fs \(vda\): mounted/),
    "the image's init run, ms after its start": printk(/^Run \/usr\/sbin\/surogate-init as init process/),
    "the sessions disk mounted, ms after its start": printk(/^EXT4-fs \(vdc\): mounted/),
    "the agent's start, ms after its start": agent?.guest,
    "the rule's outcome, ms after its start": rule?.guest,
    "the agent's hello, ms after its start": start !== undefined && hello !== undefined ? Math.round(hello - start) : undefined,
  };
  return Object.fromEntries(Object.entries(found).map(([what, value]) => [what, value ?? "not seen"]));
}

it("boots the arm64 guest to its hello, says where the time goes, and refuses the kernel gzipped", async () => {
  const first = await boot({}, true);
  const later = [];
  for (let n = 0; n < 3; n += 1) later.push(await boot({}, false));
  const logged = await boot({ agentDisk: PROBING_DISK }, false);
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
  // Signed ad hoc again, with no entitlement: Apple silicon runs no code that is not signed at all.
  const bare = join(scratch("boot-bare"), "surogate-vm");
  copyFileSync(PROGRAM, bare);
  execFileSync("/usr/bin/codesign", ["--force", "--sign", "-", bare]);
  const signed = check(PROGRAM);
  const unentitled = check(bare);
  const answer = (said: string) => {
    try {
      return JSON.parse(said) as { supported?: unknown };
    } catch {
      return {};
    }
  };
  const times = later.map(({ ms: took }) => took);
  const failed = record("boot", {
    "the first boot to hello, ms, the sessions disk made": first.ms,
    "each later boot to hello, ms, the sessions disk there": times,
    "median, ms": median(times),
    "the boot with the kernel's log to hello, ms": logged.ms,
    "where the kernel's boot went": kernelTimes(logged.timeline),
    "the first boot's timeline": told(first.timeline),
    "a later boot's timeline": told(later[1]!.timeline),
    "the boot with the kernel's log, its timeline": told(logged.timeline),
    "the boot with the kernel's log, its console, ms seen: line": logged.timeline.console.map(({ ms: seen, line }) => `${seen}: ${line}`),
    "the kernel gzipped": gzip,
    "surogate-vm check, signed": signed,
    "surogate-vm check, signed without its entitlement": unentitled,
  }, {
    "the later boots reach hello in under 1.5 s at the median": median(times) < 1_500,
    "the protected-names rule attached at every boot": [first, ...later, logged].every(({ attached }) => attached),
    "the kernel gzipped is refused, as the image keeps the Image alone": gzip.startsWith("did not boot"),
    "the signed surogate-vm says this Mac can run the guest": answer(signed).supported === true,
    "surogate-vm without its entitlement says this Mac cannot": answer(unentitled).supported === false,
  });
  expect(failed).toEqual([]);
});

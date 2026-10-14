// How long the x64 guest takes from surogate-vm.exe's start to the agent's hello on this PC, booted directly by HCS's
// LinuxKernelDirect with the image's bzImage and Hyper-V's initrd, three SCSI disks, the console on COM1, and the
// protected-names rule attached, and where that time goes. Median of three, each of a data folder of its own, so each
// makes its sessions disk as the app's first boot does; the target is under 1.5 s (Linux, under KVM: 650 to 752 ms).
// A boot that fails is written down with the rest, and the boots after it are still made. One boot more, with the
// measurements' agent disk, has its init write the kernel's own log to the console, each line with its time since the
// kernel's start, and the guest's uptime as the agent starts and as the rule's outcome lands (probe.sh). Every boot's
// timeline (harness.ts) has surogate-vm.exe's start, its lines, the boot line, the console's first byte and each of its
// lines, and the hello, or why it failed.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts boot
// PASS or FAIL: every boot reaches hello, in under 1.5 s at the median, and the rule attached at every boot.

import { readFileSync } from "node:fs";

import { expect, it } from "vitest";

import type { VmOptions } from "../../../../src/vm/manager.js";
import { BootFailed, lab, median, ms, PROBING_DISK, record, ROOTS, type Timeline, told } from "./harness.js";

const RULE = "surogate: the protected-names rule attached 16 of 16 hooks";

interface Booted {
  ms: number;
  // Why it did not reach hello, where it did not.
  failed?: string;
  attached: boolean;
  timeline: Timeline;
}

// A boot of the new data folder *name*, and the time to its hello, or to its failure.
async function boot(name: string, extra: Partial<VmOptions> = {}): Promise<Booted> {
  const begun = performance.now();
  try {
    const booted = await lab(name, extra);
    const took = ms(begun);
    await booted.run(ROOTS[0]!, "true").catch(() => undefined);
    await booted.stop();
    // Read once the guest has gone, when all it printed has reached the log.
    return { ms: took, attached: readFileSync(booted.options.console, "utf8").includes(RULE), timeline: booted.timeline };
  } catch (error) {
    const timeline = error instanceof BootFailed ? error.timeline : { begun: "", events: [], console: [] };
    return { ms: ms(begun), failed: (error as Error).message, attached: false, timeline };
  }
}

// The time of the first event of *timeline* whose words start with *what*.
const when = (timeline: Timeline, what: string) => timeline.events.find((event) => event.what.startsWith(what))?.ms;

// Where the kernel's boot went, from the console of the boot whose init wrote the kernel's log (probe.sh): each
// milestone at its time since the kernel's start, the longest waits between two of its lines, and where the kernel's
// start was on this PC's timeline.
function kernelTimes(timeline: Timeline): Record<string, unknown> {
  const printk = timeline.console.flatMap(({ line }) => {
    const at = /^\[\s*(\d+\.\d+)\]\s(.*)$/.exec(line);
    return at ? [{ ms: Math.round(Number(at[1]) * 1000), text: at[2] ?? "" }] : [];
  });
  const first = (pattern: RegExp) => printk.find(({ text }) => pattern.test(text))?.ms;
  const uptime = (what: string) => {
    for (const { ms: seen, line } of timeline.console) {
      const at = new RegExp(`^surogate-measure: uptime (\\d+\\.\\d+) s, ${what}`).exec(line);
      if (at) return { guest: Math.round(Number(at[1]) * 1000), host: seen };
    }
    return undefined;
  };
  const agent = uptime("the agent's start");
  const rule = uptime("the rule's outcome");
  // The kernel's start on this PC's timeline: when the agent's start was seen, less the guest's uptime then.
  const start = agent ? agent.host - agent.guest : undefined;
  const started = when(timeline, 'surogate-vm said {"type":"started"');
  const sent = when(timeline, "the boot line sent");
  const hello = when(timeline, "the agent's hello");
  const waits = printk.slice(1).map((line, n) => ({ ms: line.ms - printk[n]!.ms, after: printk[n]!.text, before: line.text }));
  const found: Record<string, unknown> = {
    "the kernel's start, ms on this PC's timeline (the agent's start seen, less the guest's uptime then)": start,
    "from the boot line to the kernel's start, ms": start !== undefined && sent !== undefined ? Math.round(start - sent) : undefined,
    "from surogate-vm's started to the kernel's start, ms": start !== undefined && started !== undefined ? Math.round(start - started) : undefined,
    "the initrd's init run, ms after its start": first(/^Run \/init as init process/),
    "the SCSI disks attached, ms after its start": first(/^sd \d+:\d+:\d+:2: \[sd[a-z]\] Attached SCSI disk/),
    "the image's disk mounted, ms after its start": first(/^EXT4-fs \(sd[a-z]\): mounted filesystem .* ro /),
    "the kernel's random pool ready (crng init done), ms after its start": first(/^random: crng init done/),
    "the kernel's last line before the agent's start, ms after its start": printk.filter(({ ms: at }) => agent === undefined || at <= agent.guest).at(-1)?.ms,
    "the agent's start, ms after its start": agent?.guest,
    "the rule's outcome, ms after its start": rule?.guest,
    "the agent's hello, ms after its start": start !== undefined && hello !== undefined ? Math.round(hello - start) : undefined,
    "the kernel's five longest waits between two lines, ms": [...waits].sort((a, b) => b.ms - a.ms).slice(0, 5),
  };
  return Object.fromEntries(Object.entries(found).map(([what, value]) => [what, value ?? "not seen"]));
}

// A boot as the findings say it: its time or its failure, its events, and its console, each line as it was seen.
const said = ({ ms: took, failed, timeline }: Booted) => ({
  [failed === undefined ? "to hello, ms" : "failed after, ms"]: took,
  ...(failed === undefined ? {} : { failed }),
  "asked at": timeline.begun,
  timeline: told(timeline),
  "its console, ms seen: line": timeline.console.map(({ ms: seen, line }) => `${seen}: ${line}`),
});

it("boots the x64 guest to its hello through HCS, with its initrd, and says where the time goes", async () => {
  const boots: Booted[] = [];
  for (let n = 0; n < 3; n += 1) boots.push(await boot(`boot-${n}`));
  const logged = await boot("boot-logged", { agentDisk: PROBING_DISK });
  const times = boots.filter(({ failed }) => failed === undefined).map(({ ms: took }) => took);
  const failed = record("boot", {
    "boot to hello, ms": boots.map(({ ms: took, failed: why }) => (why === undefined ? took : `failed after ${took}`)),
    "median, ms": median(times),
    "each boot": boots.map(said),
    "the boot with the kernel's log": said(logged),
    "where the kernel's boot went": kernelTimes(logged.timeline),
  }, {
    "every boot reaches hello": boots.every(({ failed: why }) => why === undefined),
    "the image boots to hello in under 1.5 s at the median": times.length > 0 && median(times) < 1_500,
    "the protected-names rule attached at every boot": boots.every(({ attached }) => attached),
  });
  expect(failed).toEqual([]);
});

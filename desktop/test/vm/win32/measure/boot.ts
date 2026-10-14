// How long the x64 guest takes from surogate-vm.exe's start to the agent's hello on this PC, booted directly by HCS's
// LinuxKernelDirect with the image's bzImage and Hyper-V's initrd, three SCSI disks, the console on COM1, and the
// protected-names rule attached, and where that time goes. The first boot makes the sessions disk (vm/init's mke2fs),
// as only the app's first boot does; the three after it find the disk there and check it, as every later boot does,
// and their median is the target's: under 1.5 s (Linux, under KVM: 650 to 752 ms). A boot that fails is written down
// with the rest, and the boots after it are still made: one refused while the last one's VM was still being ended is
// asked again. One later boot more, with the measurements' agent disk, has its init write the guest's uptime as the
// agent starts and as the rule's outcome lands, then the kernel's own log once the agent has said hello, its waits and
// milestones, each line with its time since the kernel's start (probe.sh). Every boot's timeline (harness.ts) has
// surogate-vm.exe's start, its lines, the boot line, the console's first byte and each of its lines, and the hello, or
// why it failed; and how long the image's copy took to reach the disk before the first.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts boot
// PASS or FAIL: every boot reaches hello, the later ones in under 1.5 s at the median, and the rule attached at every
// boot.

import { readFileSync } from "node:fs";

import { expect, it } from "vitest";

import type { VmOptions } from "../../../../src/vm/manager.js";
import { BootFailed, IMAGE_FLUSH_MS, lab, median, ms, PROBING_DISK, record, ROOTS, type Timeline, told } from "./harness.js";

const RULE = "surogate: the protected-names rule attached 16 of 16 hooks";
// The service's refusal of a boot while the last one's VM is still being ended (service/src/core.rs).
const ENDING = "a VM runs for this data folder already";
// The last line of the kernel's log on the console (probe.sh), and how long it is waited for after the hello.
const LOGGED = "surogate-measure: the kernel's log ends";
const LOG_MS = 60_000;

interface Booted {
  ms: number;
  // Why it did not reach hello, where it did not.
  failed?: string;
  // How long it waited for the last boot's VM to end, where the service refused it meanwhile.
  waited?: number;
  attached: boolean;
  timeline: Timeline;
}

// A boot of the data folder "boot", new for the first (*fresh*), and the time to its hello, or to its failure. One the
// service refuses while the last one's VM is still being ended is asked again every second, for a minute at most.
// *logged*: the console's kernel log is waited for after the hello, before the guest is stopped.
async function boot(extra: Partial<VmOptions>, { fresh = false, logged = false } = {}): Promise<Booted> {
  const asked = performance.now();
  for (;;) {
    const begun = performance.now();
    const waited = Math.round(begun - asked);
    try {
      const booted = await lab("boot", extra, undefined, { fresh });
      const took = ms(begun);
      if (logged) {
        const by = performance.now() + LOG_MS;
        while (!booted.timeline.console.some(({ line }) => line === LOGGED) && performance.now() < by) await new Promise((resolve) => setTimeout(resolve, 100));
      }
      await booted.run(ROOTS[0]!, "true").catch(() => undefined);
      await booted.stop();
      // Read once the guest has gone, when all it printed has reached the log.
      return { ms: took, ...(waited > 0 ? { waited } : {}), attached: readFileSync(booted.options.console, "utf8").includes(RULE), timeline: booted.timeline };
    } catch (error) {
      const why = (error as Error).message;
      if (why.includes(ENDING) && performance.now() - asked < 60_000) {
        await new Promise((resolve) => setTimeout(resolve, 1_000));
        continue;
      }
      const timeline = error instanceof BootFailed ? error.timeline : { begun: "", events: [], console: [] };
      return { ms: ms(begun), failed: why, ...(waited > 0 ? { waited } : {}), attached: false, timeline };
    }
  }
}

// The time of the first event of *timeline* whose words start with *what*.
const when = (timeline: Timeline, what: string) => timeline.events.find((event) => event.what.startsWith(what))?.ms;

// Where the kernel's boot went, from the console of the boot whose init wrote the kernel's log (probe.sh): each
// milestone at its time since the kernel's start, the longest of the waits the probe marked between two of its lines,
// and where the kernel's start was on this PC's timeline.
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
  // Each wait the probe marked, between the kernel's lines on either side of it.
  const waits: Array<{ ms: number; after: string; before: string }> = [];
  let last = "";
  for (const { line } of timeline.console) {
    const wait = /^surogate-measure: the kernel waited (\d+) ms$/.exec(line);
    const at = /^\[\s*\d+\.\d+\]\s(.*)$/.exec(line);
    if (wait) waits.push({ ms: Number(wait[1]), after: last, before: "" });
    else if (at) {
      last = at[1] ?? "";
      const open = waits.at(-1);
      if (open && open.before === "") open.before = last;
    }
  }
  const found: Record<string, unknown> = {
    "the kernel's start, ms on this PC's timeline (the agent's start seen, less the guest's uptime then)": start,
    "from the boot line to the kernel's start, ms": start !== undefined && sent !== undefined ? Math.round(start - sent) : undefined,
    "from surogate-vm's started to the kernel's start, ms": start !== undefined && started !== undefined ? Math.round(start - started) : undefined,
    "the kernel's own start done (its init memory freed), ms after its start": first(/^Freeing unused kernel image/),
    "the initrd's init run, ms after its start": first(/^Run \/init as init process/),
    "the SCSI disks attached, ms after its start": first(/^sd \d+:\d+:\d+:2: \[sd[a-z]\] Attached SCSI disk/),
    "the image's disk mounted, ms after its start": first(/^EXT4-fs \(sd[a-z]\): mounted filesystem .* ro /),
    "the sessions disk mounted, ms after its start": first(/^EXT4-fs \(sd[a-z]\): mounted filesystem .* r\/w /),
    "the kernel's random pool ready (crng init done), ms after its start": first(/^random: crng init done/),
    "the kernel's last line before the agent's start, ms after its start": printk.filter(({ ms: at }) => agent === undefined || at <= agent.guest).at(-1)?.ms,
    "the agent's start, ms after its start": agent?.guest,
    "the rule's outcome, ms after its start": rule?.guest,
    "the agent's hello, ms after its start": start !== undefined && hello !== undefined ? Math.round(hello - start) : undefined,
    "the kernel's eight longest waits between two lines, ms": [...waits].sort((a, b) => b.ms - a.ms).slice(0, 8),
    "the kernel's waits of 50 ms or more, ms in all": waits.reduce((sum, { ms: wait }) => sum + wait, 0),
  };
  return Object.fromEntries(Object.entries(found).map(([what, value]) => [what, value ?? "not seen"]));
}

// A boot as the findings say it: its time or its failure, its events, and its console, each line as it was seen.
const said = ({ ms: took, failed, waited, timeline }: Booted) => ({
  [failed === undefined ? "to hello, ms" : "failed after, ms"]: took,
  ...(failed === undefined ? {} : { failed }),
  ...(waited === undefined ? {} : { "waited first for the last boot's VM to end, ms": waited }),
  "asked at": timeline.begun,
  timeline: told(timeline),
  "its console, ms seen: line": timeline.console.map(({ ms: seen, line }) => `${seen}: ${line}`),
});

it("boots the x64 guest to its hello through HCS, with its initrd, and says where the time goes", async () => {
  const first = await boot({}, { fresh: true });
  const later: Booted[] = [];
  for (let n = 0; n < 3; n += 1) later.push(await boot({}));
  const logged = await boot({ agentDisk: PROBING_DISK }, { logged: true });
  const boots = [first, ...later];
  const times = later.filter(({ failed }) => failed === undefined).map(({ ms: took }) => took);
  const toHello = ({ ms: took, failed: why }: Booted) => (why === undefined ? took : `failed after ${took}`);
  const failed = record("boot", {
    "the image's copy, made by this measurement, on disk before the first boot, ms": IMAGE_FLUSH_MS ?? "copied before this measurement",
    "the first boot to hello, ms, the sessions disk made": toHello(first),
    "each later boot to hello, ms, the sessions disk there": later.map(toHello),
    "median, ms": median(times),
    "each boot": boots.map(said),
    "the boot with the kernel's log": said(logged),
    "where the kernel's boot went": kernelTimes(logged.timeline),
  }, {
    "every boot reaches hello": [...boots, logged].every(({ failed: why }) => why === undefined),
    "the later boots reach hello in under 1.5 s at the median": times.length === later.length && median(times) < 1_500,
    "the protected-names rule attached at every boot": boots.every(({ attached }) => attached),
  });
  expect(failed).toEqual([]);
});

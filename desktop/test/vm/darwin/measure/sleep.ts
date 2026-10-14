// A real sleep of this Mac during a command: the guest's clock after the wake, the keepalive (the guest is
// not lost for the pings it missed asleep), and the command's own timeout kept on the host's monotonic
// clock, which on macOS does not count the sleep (mach_absolute_time). It is the VM manager's resume, as
// Electron's powerMonitor calls it at a wake, that tells the guest the time (src/vm/manager.ts).
// App Nap is the app's alone, hidden for an hour: that check is by hand, with the app.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts sleep
// When it says so, put this Mac to sleep (Apple menu, Sleep, or close the lid) for a minute or more, then
// wake it; it waits ten minutes for that, a sleep that runs past them counted whole. PASS, FAIL or UNTESTED: the
// command went on and ended well, the guest was not lost, and the guest's clock is within 2 s of this Mac's after the
// wake.

import { expect, it } from "vitest";

import { judged, lab, type Ran, ready, record, ROOTS, scratch } from "./harness.js";

// How far this Mac's wall clock has got past its monotonic one: it grows by each sleep.
const offset = () => Date.now() - performance.now();
// How far the guest's clock is past this Mac's, by a command's date, and that command.
const guestClock = async (run: (command: string) => Promise<Ran>) => {
  const asked = Date.now();
  const read = await run("date +%s%3N");
  return { ms: Number(read.output.trim()) - (asked + Date.now()) / 2, read };
};

it("goes on with a command over a sleep of this Mac", async () => {
  const booted = await lab("sleep");
  const root = ROOTS[0]!;
  await ready(booted.guest, root, scratch("sleep"));
  const run = (command: string) => booted.run(root, command, 1_200);
  const command = run("begun=$(date +%s); sleep 240; echo \"$begun $(date +%s)\"");
  const before = offset();
  console.log("MEASURE sleep: put this Mac to sleep now, for a minute or more, then wake it");
  // The offset read again before the window is looked at: a sleep that ends past the window's end is the one waited for.
  let slept = 0;
  for (const end = Date.now() + 600_000; ; await new Promise((resolve) => setTimeout(resolve, 1_000))) {
    slept = offset() - before;
    if (slept >= 30_000 || Date.now() >= end) break;
  }
  booted.guest.resume();
  await new Promise((resolve) => setTimeout(resolve, 3_000));
  const drift = await guestClock(run);
  const ended = await command;
  await new Promise((resolve) => setTimeout(resolve, 120_000));
  const later = await guestClock(run);
  const lost = booted.guest.lost;
  await booted.stop();
  const failed = record("sleep", {
    "slept, s": Math.round(slept / 1000), "the command's answer": ended.outcome, "the guest's clock past this Mac's after the wake, ms": Math.round(drift.ms),
    "and two minutes later, ms": Math.round(later.ms),
  }, {
    "this Mac slept for 30 s or more": slept >= 30_000,
    "the command went on and ended well": judged([ended], ended.returncode === 0),
    "the guest was not lost": !lost,
    "the guest's clock is within 2 s of this Mac's after the wake": judged([drift.read], Math.abs(drift.ms) < 2_000),
  });
  expect(failed).toEqual([]);
});

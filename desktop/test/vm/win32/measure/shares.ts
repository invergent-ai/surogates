// Plan 9 shares added and removed while the guest runs (HcsModifyComputeSystem): how long a share and its removal
// take, 40 cycles with two roots, eight at once, and a removal while the guest still holds the mount. Decides the
// share's deadline and what a held share does.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts shares
// PASS or FAIL: 40 cycles each read their own folder, and a removal of a held share ends the VM by the deadline.

import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { lab, median, ms, ready, record, ROOTS, scratch, signal } from "./harness.js";

const background = (command: string) => ({ command, workdir: null, task_id: "held", pty: false, notify_on_complete: false, watcher_interval: null });

it("adds and removes Plan 9 shares while the guest runs", async () => {
  const booted = await lab("shares");
  const [one, two] = ROOTS as [string, string, ...string[]];
  const a = scratch("shares-a");
  const b = scratch("shares-b");
  writeFileSync(join(a, "a.txt"), "a");
  writeFileSync(join(b, "b.txt"), "b");
  const adds: number[] = [];
  const reads: boolean[] = [];
  for (let n = 0; n < 40; n += 1) {
    let begun = performance.now();
    await ready(booted.guest, one, a);
    adds.push(ms(begun));
    reads.push((await booted.run(one, "cat a.txt")).output === "a");
    await ready(booted.guest, two, b);
    reads.push((await booted.run(two, "cat b.txt")).output === "b");
    begun = performance.now();
    await booted.guest.teardown(one);
    await booted.guest.teardown(two);
  }
  // A folder held by a running process, taken out under it.
  await ready(booted.guest, one, a);
  await booted.guest.op(one, "start", background("sleep 600"), signal());
  const begun = performance.now();
  const held = await booted.guest.teardown(one).then(() => `let go in ${ms(begun)} ms`, (error: Error) => `the VM ended: ${error.message}`);
  await booted.stop().catch(() => undefined);
  const failed = record("shares", {
    "a share and its setup, ms, median of 40": median(adds), "a held share taken out": held,
  }, {
    "every one of 40 cycles read its own folder, for two roots": reads.length === 80 && reads.every(Boolean),
  });
  expect(failed).toEqual([]);
});

// The guest's named console ports on Apple's framework: the names the guest sees in
// /sys/class/virtio-ports (the agent finds its ports by them, src/guest/port.ts); a command's round trip
// through the control port and a 261 KB result through it, as Linux's measured; and what the VM does
// when the host's end of a port's socket ends. Decides that named ports carry the guest's transport,
// or a vsock bridge in the guest.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts ports
// PASS or FAIL: the three names are there, and the 261 KB come back whole. The rest is written down.

import { expect, it } from "vitest";

import { lab, median, ms, ready, record, ROOTS, scratch } from "./harness.js";

it("carries the agent's ports by their names, whole", async () => {
  const booted = await lab("ports");
  await ready(booted.guest, ROOTS[0]!, scratch("ports"));
  const listed = await booted.run(ROOTS[0]!, 'for p in /sys/class/virtio-ports/*; do printf "%s %s\\n" "${p##*/}" "$(cat "$p/name" 2>/dev/null)"; done');
  const trips: number[] = [];
  for (let n = 0; n < 30; n += 1) {
    const begun = performance.now();
    await booted.run(ROOTS[0]!, "true");
    trips.push(ms(begun));
  }
  let begun = performance.now();
  const large = await booted.run(ROOTS[0]!, "head -c 195750 /dev/zero | base64 -w0");
  const largeMs = ms(begun);
  // The host's end of the inbound port's socket ended: does the guest, and the VM, go on?
  booted.vm.inbound.destroy();
  await new Promise((resolve) => setTimeout(resolve, 2_000));
  begun = performance.now();
  const after = await booted.run(ROOTS[0]!, "echo still here", 20);
  const gone = await Promise.race([booted.vm.exited.then((said) => `the VM went: ${said}`), new Promise<string>((resolve) => setTimeout(() => resolve("the VM runs on"), 100))]);
  const names = listed.output.trim().split("\n");
  const failed = record("ports", {
    "ports the guest sees": names, "round trip of a command, ms, median of 30": median(trips), "261 KB result, ms": largeMs,
    "261 KB result's length": large.output.length,
    "the inbound port's host end ended, then a command": `${JSON.stringify(after.output.trim())} in ${ms(begun)} ms; ${gone}`,
  }, {
    "the guest sees the ports ai.surogate.control, ai.surogate.net and ai.surogate.inbound": ["ai.surogate.control", "ai.surogate.net", "ai.surogate.inbound"].every((name) => names.some((line) => line.endsWith(` ${name}`))),
    "a 261 KB result comes back whole": large.output.length === 261_000 && /^A+$/.test(large.output),
  });
  await booted.stop().catch(() => {});
  expect(failed).toEqual([]);
});

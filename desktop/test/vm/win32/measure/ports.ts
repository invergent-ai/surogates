// The guest's three ports on Hyper-V sockets, relayed to surogate-vm.exe's named pipes (native/windows/vm): a
// command's round trip through the control port, a 261 KB result through it, and what the VM does when the host's end
// of the inbound port's pipe ends. Decides the relay's design.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts ports
// PASS or FAIL: the 261 KB come back whole. The round trip and the ended connection are written down.

import { expect, it } from "vitest";

import { lab, median, ms, ready, record, ROOTS, scratch } from "./harness.js";

it("carries the agent's ports through the pipes, whole", async () => {
  const booted = await lab("ports");
  await ready(booted.guest, ROOTS[0]!, scratch("ports"));
  const trips: number[] = [];
  for (let n = 0; n < 30; n += 1) {
    const begun = performance.now();
    await booted.run(ROOTS[0]!, "true");
    trips.push(ms(begun));
  }
  let begun = performance.now();
  const large = await booted.run(ROOTS[0]!, "head -c 195750 /dev/zero | base64 -w0");
  const largeMs = ms(begun);
  booted.vm.inbound.destroy();
  await new Promise((resolve) => setTimeout(resolve, 2_000));
  begun = performance.now();
  const after = await booted.run(ROOTS[0]!, "echo still here", 20);
  const gone = await Promise.race([booted.vm.exited.then((said) => `the VM went: ${said}`), new Promise<string>((resolve) => setTimeout(() => resolve("the VM runs on"), 100))]);
  const failed = record("ports", {
    "round trip of a command, ms, median of 30": median(trips), "261 KB result, ms": largeMs, "261 KB result's length": large.output.length,
    "the inbound port's host end ended, then a command": `${JSON.stringify(after.output.trim())} in ${ms(begun)} ms; ${gone}`,
  }, {
    "a 261 KB result comes back whole": large.output.length === 261_000 && /^A+$/.test(large.output),
  });
  await booted.stop().catch(() => undefined);
  expect(failed).toEqual([]);
});

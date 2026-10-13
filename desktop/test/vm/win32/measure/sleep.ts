// A real sleep during a command (Mw10), by hand: on a modern-standby laptop and an S3 desktop, and a hibernate, the
// guest's clock, the link's keepalive and the command's timeout. Hyper-V's VMs stay in memory through S3 and modern
// standby, and powerMonitor's resume fires on Windows, so the keepalive reset and the time message are unchanged.
// This starts a command that sleeps the guest then reports its clock; the tester sleeps the PC while it runs.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts sleep
// By hand: start it, put the PC to sleep for a minute, wake it, and read what it wrote.

import { expect, it } from "vitest";

import { lab, ready, record, ROOTS, scratch } from "./harness.js";

it("keeps a command going across a real sleep", async () => {
  const booted = await lab("sleep");
  const root = ROOTS[0]!;
  await ready(booted.guest, root, scratch("sleep"));
  const before = (await booted.run(root, "date -u +%s")).output.trim();
  console.log("MEASURE sleep: put the PC to sleep now for about a minute, then wake it");
  const after = await booted.run(root, "sleep 120; date -u +%s; echo still here", 300);
  await booted.stop();
  const wall = Number(after.output.trim().split("\n")[0]) - Number(before);
  record("sleep", { "the command after the wake": after.output.trim(), "wall-clock seconds the command saw": wall }, {});
  expect(after.output).toContain("still here");
});

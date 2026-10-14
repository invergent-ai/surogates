// The host memory a 2 GiB guest holds over a working day, with the cold discard hint and the page reporting
// order WSL picks (native/windows/hcs): vmmem's working set idle, at a command's 1.5 GiB peak, and each minute after
// it ends, for MEASURE_MINUTES (20 unless set). Decides a ceiling to write down.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts memory
// Written down, no PASS or FAIL: the working sets, in MiB.

import { expect, it } from "vitest";

import { lab, ready, record, ROOTS, scratch, windowsPowerShell } from "./harness.js";

const vmmemMiB = () => {
  try {
    const bytes = windowsPowerShell("(Get-Process -Name vmmem -ErrorAction SilentlyContinue | Measure-Object WorkingSet64 -Sum).Sum");
    return Math.round(Number(bytes || 0) / 1024 / 1024);
  } catch {
    return 0;
  }
};

it("holds the guest's memory over time", async () => {
  const booted = await lab("memory");
  const root = ROOTS[0]!;
  await ready(booted.guest, root, scratch("memory"));
  const idle = vmmemMiB();
  const peak = booted.run(root, "python3 -c 'import time; a = bytearray(1536 * 1024 * 1024); a[::4096] = b\"x\" * len(a[::4096]); time.sleep(20)'", 300);
  await new Promise((resolve) => setTimeout(resolve, 15_000));
  const atPeak = vmmemMiB();
  await peak;
  const after: number[] = [];
  for (let minute = 0; minute < Number(process.env.MEASURE_MINUTES ?? 20); minute += 1) {
    await new Promise((resolve) => setTimeout(resolve, 60_000));
    after.push(vmmemMiB());
  }
  await booted.stop();
  record("memory", { "idle, MiB": idle, "at the 1.5 GiB peak, MiB": atPeak, "each minute after, MiB": after });
  expect(atPeak).toBeGreaterThan(0);
});

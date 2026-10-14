// Microsoft Defender's cost: the image's unpack, the first boot and npm install in a share, with real-time
// protection on, and with a test exclusion set by hand. The plan adds no exclusion; this measures whether the owner
// is asked about one. Run it twice: once with real-time protection on, once with a hand-set exclusion of the data and
// share folders, and compare. It records Defender's state and the boot and install times.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts defender
// Written down, no PASS or FAIL: the times with protection on and with the exclusion.

import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { lab, ms, ready, record, ROOTS, scratch, windowsPowerShell } from "./harness.js";

const defender = () => {
  try {
    return windowsPowerShell("(Get-MpComputerStatus).RealTimeProtectionEnabled");
  } catch (error) {
    return `?: ${(error as Error).message}`;
  }
};

it("measures Defender's cost on the unpack, the boot and an install", async () => {
  const realtime = defender();
  const folder = scratch("defender");
  let begun = performance.now();
  const booted = await lab("defender");
  const bootMs = ms(begun);
  await ready(booted.guest, ROOTS[0]!, folder);
  writeFileSync(join(folder, "package.json"), JSON.stringify({ name: "probe", version: "0.0.0", dependencies: { "left-pad": "1.3.0" } }));
  begun = performance.now();
  const install = (await booted.run(ROOTS[0]!, "npm install --no-audit --no-fund 2>&1 | tail -n 2 || echo 'no npm'", 600)).output.trim();
  const installMs = ms(begun);
  await booted.stop();
  record("defender", { "real-time protection": realtime, "boot to hello, ms": bootMs, "npm install, ms": installMs, "npm install, its tail": install });
  expect(bootMs).toBeGreaterThan(0);
});

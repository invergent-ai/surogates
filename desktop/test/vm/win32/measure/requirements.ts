// What this computer says of its sandbox when something is missing (Mw14's VM half, C5): Virtual Machine Platform
// off; virtualization off in the firmware; a VM without nested virtualization. Record each one's HCS answer and the
// words the backend gives. Run this on a PC in each of those states (the test day sets them); it reads surogate-vm
// check and a boot attempt, and records both.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts requirements
// Written down: surogate-vm check's facts, the backend's requirement words, and HCS's own words at a boot attempt.

import { expect, it } from "vitest";

import { checkWin32, requirementsWin32 } from "../../../../src/vm/win32.js";
import { lab, record } from "./harness.js";

it("says what this computer's sandbox lacks, and HCS's words at a boot attempt", async () => {
  const facts = await checkWin32();
  const words = await requirementsWin32(() => Promise.resolve(facts));
  let boot = "not attempted";
  try {
    const booted = await lab("requirements");
    boot = "booted";
    await booted.stop();
  } catch (error) {
    boot = (error as Error).message;
  }
  record("requirements", { "surogate-vm check": facts, "the backend's requirement words": words, "a boot attempt": boot });
  expect(Array.isArray(words)).toBe(true);
});

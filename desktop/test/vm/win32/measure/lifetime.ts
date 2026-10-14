// What is left behind when a VM ends each way: surogate-vm.exe killed, the VM manager killed, the app ended
// from Task Manager, the service killed and restarted. Look for a vmmem or vmwp left, a pipe, or an app process.
// Decides whether anything needs a sweep (the service ends a VM at its next boot for the data root, by the VM's ID).
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts lifetime
// PASS or FAIL: nothing of the VM is left after surogate-vm.exe is killed.

import { expect, it } from "vitest";

import { lab, ready, record, ROOTS, scratch, windowsPowerShell } from "./harness.js";

const running = (name: string) => {
  try {
    return windowsPowerShell(`@(Get-Process -Name '${name}' -ErrorAction SilentlyContinue).Count`);
  } catch {
    return "?";
  }
};

it("leaves nothing of the VM when surogate-vm.exe is killed", async () => {
  const booted = await lab("lifetime");
  await ready(booted.guest, ROOTS[0]!, scratch("lifetime"));
  const before = { vmmem: running("vmmem"), vmwp: running("vmwp") };
  const child = (booted.vm as unknown as { helper: { child: { pid: number; kill(signal: string): void } } }).helper.child;
  const pid = child.pid;
  child.kill("SIGKILL");
  await booted.vm.exited.catch(() => undefined);
  await new Promise((resolve) => setTimeout(resolve, 5_000));
  const after = { vmmem: running("vmmem"), vmwp: running("vmwp") };
  await booted.stop().catch(() => undefined);
  const failed = record("lifetime", { "surogate-vm.exe pid": pid, "vmmem/vmwp before": before, "vmmem/vmwp 5 s after the kill": after }, {
    "no vmwp is left after surogate-vm.exe is killed": after.vmwp === "0" || after.vmwp === before.vmwp,
  });
  expect(failed).toEqual([]);
});

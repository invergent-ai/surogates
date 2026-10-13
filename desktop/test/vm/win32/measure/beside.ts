// Beside the other virtualization a PC may run: WSL2 and Docker Desktop running; VirtualBox and VMware
// Workstation installed; Memory Integrity and Credential Guard on; beside Windows Sandbox. Nothing fails if all pass.
// This boots the guest and runs a command, and records what else the PC is running, for the tester to read beside the
// matrix of what was on.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts beside
// PASS or FAIL: the guest boots and runs a command with whatever else is on.

import { execFileSync } from "node:child_process";

import { expect, it } from "vitest";

import { lab, ready, record, ROOTS, scratch } from "./harness.js";

const ps = (command: string) => {
  try {
    return execFileSync("powershell.exe", ["-NoProfile", "-Command", command], { encoding: "utf8" }).trim();
  } catch (error) {
    return `?: ${(error as Error).message}`;
  }
};

it("boots the guest beside other virtualization", async () => {
  const booted = await lab("beside");
  await ready(booted.guest, ROOTS[0]!, scratch("beside"));
  const ran = (await booted.run(ROOTS[0]!, "echo hello from the guest")).output.trim();
  await booted.stop();
  const failed = record("beside", {
    "WSL running": ps("@(Get-Process -Name wsl,wslhost,vmwp -ErrorAction SilentlyContinue).Count"),
    "Docker running": ps("@(Get-Process -Name 'com.docker.backend','Docker Desktop' -ErrorAction SilentlyContinue).Count"),
    "Device Guard / Credential Guard": ps("(Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root/Microsoft/Windows/DeviceGuard).SecurityServicesRunning -join ','"),
    "Windows Sandbox feature": ps("(Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM).State"),
    "the guest ran a command": ran,
  }, {
    "the guest boots and runs a command beside whatever is on": ran === "hello from the guest",
  });
  expect(failed).toEqual([]);
});

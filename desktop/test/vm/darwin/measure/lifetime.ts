// What of the VM is left when what runs it is killed. Apple does not document whether the framework's own
// process for a VM (com.apple.Virtualization.VirtualMachine) ends with the process that made it. Two kills:
// surogate-vm itself by SIGKILL; and the VM manager by SIGKILL, whose end ends surogate-vm's stdin, which
// stops the VM. Decides whether the next boot's sweep must also end the framework's process, by a pidfile
// surogate-vm would write for it too.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts lifetime
// PASS or FAIL: after each kill, nothing of that VM is left: no surogate-vm, and no new framework process.

import { execFileSync, spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { runtimeFolder } from "../../../../src/vm/darwin.js";
import { AGENT_DISK, DESKTOP, IMAGE, lab, PROGRAM, record, USER } from "./harness.js";

// The framework's VM processes now, by pid.
const vms = () => {
  try {
    return execFileSync("/usr/bin/pgrep", ["-f", "com.apple.Virtualization.VirtualMachine"], { encoding: "utf8" }).trim().split("\n").filter(Boolean).map(Number);
  } catch {
    return [];
  }
};
const alive = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 5_000));

it("leaves nothing of a VM whose surogate-vm, or whose manager, was killed", async () => {
  const before = vms();
  const booted = await lab("lifetime");
  const ran = vms().filter((pid) => !before.includes(pid));
  const helper = Number(readFileSync(join(booted.options.run, "vm.pid"), "utf8"));
  process.kill(helper, "SIGKILL");
  await settle();
  const left = ran.filter(alive);
  await booted.vm.kill();

  // A manager of its own, booted from dist/, then killed.
  const data = join(booted.data, "manager");
  const options = {
    kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: AGENT_DISK, sessions: join(data, "vm", "sessions.img"),
    console: join(data, "logs", "vm-console.log"), run: runtimeFolder(data), user: USER, program: PROGRAM, data,
  };
  const darwin = join(DESKTOP, "dist", "vm", "darwin.js");
  const between = vms();
  const manager = spawn(process.execPath, ["--input-type=module", "-e", [
    'import { spawn } from "node:child_process";', 'import { mkdirSync } from "node:fs";',
    `const { darwinBoot } = await import(${JSON.stringify(darwin)});`,
    `mkdirSync(${JSON.stringify(join(data, "logs"))}, { recursive: true });`,
    `await darwinBoot((program, args, options) => spawn(program, args, options))(${JSON.stringify(options)}, undefined, performance.now() + 20000);`,
    'console.log("booted"); setInterval(() => {}, 1000);',
  ].join("\n")], { stdio: ["ignore", "pipe", "inherit"] });
  await new Promise<void>((resolve) => manager.stdout!.on("data", (chunk: Buffer) => chunk.toString().includes("booted") && resolve()));
  const second = Number(readFileSync(join(options.run, "vm.pid"), "utf8"));
  const secondVms = vms().filter((pid) => !between.includes(pid));
  manager.kill("SIGKILL");
  await settle();
  const failed = record("lifetime", {
    "the framework's processes for the first VM": ran, "left after surogate-vm was killed": left,
    "the framework's processes for the second": secondVms, "left after its manager was killed": [second, ...secondVms].filter(alive),
  }, {
    "nothing of the VM is left after surogate-vm is killed": left.length === 0,
    "surogate-vm ends when its manager is killed": !alive(second),
    "nothing of the VM is left after its manager is killed": secondVms.filter(alive).length === 0,
  });
  expect(failed).toEqual([]);
});

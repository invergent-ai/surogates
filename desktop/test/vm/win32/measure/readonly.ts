// The read-only share: a folder's place shares the folder itself to the guest's root with ReadOnly (native/windows/
// hcs, flags). The guest's root, as the measurements' agent disk runs it, tries everything that writes: a remount
// read-write, a write, chmod, setxattr, truncate, hard and symbolic links, a rename, an unlink and a mkdir. Decides
// that a place's folder stays safe from the guest's root.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts readonly
// A write that reaches the folder is a SECURITY finding: STOP and report it.

import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { identityOf } from "../../../../src/files/identity.js";
import { lab, PROBING_DISK, ready, record, ROOTS, scratch } from "./harness.js";

const KEY = "0123456789abcdef";
const ATTEMPTS: Array<[string, string]> = [
  ["write", 'sh -c "echo x > $at/made"'], ["chmod", "chmod 777 $at/real.txt"], ["truncate", "truncate -s 0 $at/real.txt"],
  ["hardlink", "ln $at/real.txt $at/linked"], ["symlink", "ln -s /etc/passwd $at/planted"], ["rename", "mv $at/real.txt $at/moved"],
  ["unlink", "rm $at/real.txt"], ["mkdir", "mkdir $at/made-dir"],
];

it("keeps a place's folder read-only to the guest's root", async () => {
  const real = scratch("readonly");
  writeFileSync(join(real, "real.txt"), "real\n");
  const before = { names: readdirSync(real).sort(), text: readFileSync(join(real, "real.txt"), "utf8") };
  const booted = await lab("readonly", { agentDisk: PROBING_DISK });
  const history = join(booted.data, "history", KEY);
  mkdirSync(history, { recursive: true });
  const placed = await booted.guest.place({ key: KEY, history, real: { path: real, ...identityOf(statSync(real)) } });
  if (placed) {
    await booted.stop();
    expect(record("readonly", { "the place": placed }, { "the place was mounted": false })).toEqual([]);
    return;
  }
  await ready(booted.guest, ROOTS[0]!, scratch("readonly-root"));
  const script = [
    `for at in /run/surogate/places/${KEY}/real; do`,
    '  echo "at $at"; echo "remount: $(mount -o remount,rw "$at" 2>&1 && echo allowed || echo refused)"',
    ...ATTEMPTS.map(([what, command]) => `  echo "${what}: $(${command} 2>&1 && echo allowed || echo refused)"`),
    "done",
  ].join("\n");
  writeFileSync(join(history, ".measure-probe"), `${script}\n`);
  let said = "";
  for (const end = Date.now() + 30_000; Date.now() < end && !said; await new Promise((resolve) => setTimeout(resolve, 200))) {
    try {
      said = readFileSync(join(history, ".measure-probe.out"), "utf8");
    } catch {
      // Not yet.
    }
  }
  await booted.stop();
  const after = { names: readdirSync(real).sort(), text: readFileSync(join(real, "real.txt"), "utf8") };
  const lines = said.trim().split("\n");
  const failed = record("readonly", { "what the guest's root tried": lines, "the folder after": after }, {
    "the probe ran": lines.some((line) => line.startsWith("at ")),
    "every write refused": lines.filter((line) => !line.startsWith("at ") && !line.startsWith("remount")).every((line) => line.endsWith("refused")),
    "the folder on the host as it was": JSON.stringify(after) === JSON.stringify(before),
  });
  expect(failed).toEqual([]);
});

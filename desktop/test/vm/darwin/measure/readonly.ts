// The read-only share: a folder's place (src/vm/manager.ts, Guest.place) shares the folder itself to the
// guest's root readOnly (VZSharedDirectory(url:readOnly: true)), in place of the bwrap Linux serves it from.
// The guest's root, as the measurements' agent disk runs it (prepare.sh), tries everything that writes:
// a remount read-write, a write, an overwrite, chmod, setxattr, truncate, hard and symbolic links, a rename,
// an unlink and a mkdir, on the place's mount and on the device's own directory. Decides that a place's
// folder stays safe from the guest's root.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts readonly
// PASS or FAIL: every write refused, and the folder on the host as it was.

import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { folderOf, lab, PROBING_DISK, record, ROOTS, scratch } from "./harness.js";

const KEY = "0123456789abcdef";
const ATTEMPTS = [
  ["write", 'sh -c "echo x > $at/made"'], ["overwrite", 'sh -c "echo x > $at/real.txt"'], ["chmod", "chmod 777 $at/real.txt"],
  ["setxattr", "python3 -c \"import os, sys; os.setxattr(sys.argv[1], 'user.x', b'1')\" $at/real.txt"], ["truncate", "truncate -s 0 $at/real.txt"],
  ["hard link", "ln $at/real.txt $at/linked"], ["symbolic link", "ln -s /etc/passwd $at/planted"], ["rename", "mv $at/real.txt $at/moved"],
  ["unlink", "rm $at/real.txt"], ["mkdir", "mkdir $at/made-dir"],
];

it("keeps a place's folder read-only to the guest's root, however it remounts", async () => {
  const real = scratch("readonly");
  writeFileSync(join(real, "real.txt"), "real\n", { mode: 0o644 });
  const before = { names: readdirSync(real).sort(), text: readFileSync(join(real, "real.txt"), "utf8"), mode: statSync(join(real, "real.txt")).mode };
  const booted = await lab("readonly", { agentDisk: PROBING_DISK });
  const history = join(booted.data, "history", KEY);
  mkdirSync(history, { recursive: true, mode: 0o700 });
  const { dev, ino } = statSync(real);
  const placed = await booted.guest.place({ key: KEY, history, real: { path: real, dev, ino } });
  if (placed) {
    await booted.stop();
    expect(record("readonly", { "the place": placed }, { "the place was mounted": false })).toEqual([]);
    return;
  }
  // A root of its own, so the guest has a share the probe's watcher looks in: the history's, where the probe is left.
  // Set up, or refused for the owner its folder shows: the agent mounts the share before it reads that.
  await booted.guest.ready(ROOTS[0]!, folderOf(scratch("readonly-root")));
  const script = [
    `for at in /run/surogate/places/${KEY}/real /run/surogate/shares/surogate/r2; do`,
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
  const after = { names: readdirSync(real).sort(), text: readFileSync(join(real, "real.txt"), "utf8"), mode: statSync(join(real, "real.txt")).mode };
  const lines = said.trim().split("\n");
  const failed = record("readonly", { "what the guest's root tried": lines, "the folder after": after }, {
    "the probe ran": lines.some((line) => line.startsWith("at ")),
    "every write refused": lines.filter((line) => !line.startsWith("at ") && !line.startsWith("remount")).every((line) => line.endsWith("refused")),
    "the folder on the host as it was": JSON.stringify(after) === JSON.stringify(before),
  });
  expect(failed).toEqual([]);
});

// The guest's three disks on Hyper-V: the SCSI addresses it sees (the init finds the image at :0:0:0, the agent disk
// at :0:0:1 and the sessions disk at :0:0:2, since the guest has no udev), which are read-only, and whether the
// sessions disk takes discard (UNMAP). Also that the sparse image is taken with AlwaysAllowSparseFiles (native/
// windows/hcs). Decides the init's lookup (Mw13).
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts disks
// PASS or FAIL: the three addresses and read-only flags are Linux's SCSI order.

import { expect, it } from "vitest";

import { lab, ready, record, ROOTS, scratch } from "./harness.js";

it("names the guest's SCSI disks at :0:0:0, :0:0:1 and :0:0:2", async () => {
  const booted = await lab("disks");
  await ready(booted.guest, ROOTS[0]!, scratch("disks"));
  const listed = await booted.run(ROOTS[0]!, [
    "for d in /sys/class/scsi_disk/*; do",
    '  addr="${d##*/}"; blk=$(ls "$d/device/block" 2>/dev/null)',
    '  printf "%s block=%s ro=%s discard=%s\\n" "$addr" "$blk" "$(cat /sys/block/$blk/ro 2>/dev/null)" "$(cat /sys/block/$blk/queue/discard_max_bytes 2>/dev/null)"',
    "done",
  ].join("\n"));
  await booted.stop();
  const disks = new Map(listed.output.trim().split("\n").filter(Boolean).map((line) => [line.split(" ")[0]!, Object.fromEntries(line.split(" ").slice(1).map((pair) => pair.split("=")))]));
  const at = (lun: string) => [...disks.entries()].find(([addr]) => addr.endsWith(`:0:0:${lun}`))?.[1];
  const failed = record("disks", { disks: Object.fromEntries(disks) }, {
    "three disks, at :0:0:0, :0:0:1 and :0:0:2": [at("0"), at("1"), at("2")].every(Boolean),
    "the image and the agent disk read-only, the sessions disk not": [at("0")?.ro, at("1")?.ro, at("2")?.ro].join(" ") === "1 1 0",
    "the sessions disk takes discard": Number(at("2")?.discard ?? 0) > 0,
  });
  expect(failed).toEqual([]);
});

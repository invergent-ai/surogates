// The guest's three disks on Apple's framework: the order it names them in (the image vda, the agent disk
// vdb, the sessions disk vdc, as the kernel's root=/dev/vda and vm/init take them), the serials surogate-vm
// gives them (VZVirtioBlockDeviceConfiguration.blockDeviceIdentifier), which disks are read-only, and
// whether the sessions disk takes discard. Decides whether the guest's init must find the disks by serial.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts disks
// PASS or FAIL: the order and read-only disks are Linux's. The serials and discard are written down.

import { expect, it } from "vitest";

import { lab, ready, record, ROOTS, scratch } from "./harness.js";

it("names the guest's disks in Linux's order", async () => {
  const booted = await lab("disks");
  await ready(booted.guest, ROOTS[0]!, scratch("disks"));
  const listed = await booted.run(ROOTS[0]!, 'for d in /sys/block/vd*; do printf "%s serial=%s ro=%s sectors=%s discard=%s\\n" "${d##*/}" "$(cat "$d/serial" 2>/dev/null)" "$(cat "$d/ro")" "$(cat "$d/size")" "$(cat "$d/queue/discard_max_bytes")"; done');
  await booted.stop();
  const disks = new Map(listed.output.trim().split("\n").map((line) => [line.split(" ")[0]!, Object.fromEntries(line.split(" ").slice(1).map((pair) => pair.split("=")))]));
  const failed = record("disks", { disks: Object.fromEntries(disks) }, {
    "vda, vdb and vdc, and no other": [...disks.keys()].join(" ") === "vda vdb vdc",
    "the image and the agent disk read-only, the sessions disk not": [disks.get("vda")?.ro, disks.get("vdb")?.ro, disks.get("vdc")?.ro].join(" ") === "1 1 0",
    "the sessions disk is the 32 GiB one": disks.get("vdc")?.sectors === String((32 * 1024 ** 3) / 512),
  });
  expect(failed).toEqual([]);
});

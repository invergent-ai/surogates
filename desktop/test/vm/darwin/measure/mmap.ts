// A shared map (mmap MAP_SHARED) of a file in a share, which Linux's virtiofsd refuses (ENODEV, with no
// --allow-mmap), since a host program and the guest writing one file through maps lost data there: does
// Apple's server refuse it, and if not, does a write through it reach the host, and a host write the map?
// Decides what the agent's note to the model says of databases in a folder.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts mmap
// Written down, no PASS or FAIL: "refused" is Linux's behaviour; "allowed" needs the note.

import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { lab, ready, record, ROOTS, scratch } from "./harness.js";

const MAP = (body: string) => `python3 - <<'PY'\nimport mmap, os\ntry:\n    f = open("mapped.bin", "r+b")\n    m = mmap.mmap(f.fileno(), 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)\n${body}\nexcept OSError as error:\n    print("refused", error.errno, os.strerror(error.errno))\nPY`;

it("says what a shared map of a shared file does", async () => {
  const folder = scratch("mmap");
  writeFileSync(join(folder, "mapped.bin"), Buffer.alloc(4096));
  const booted = await lab("mmap");
  await ready(booted.guest, ROOTS[0]!, folder);
  const written = (await booted.run(ROOTS[0]!, MAP('    m[0:5] = b"guest"\n    m.flush()\n    print("allowed")'))).output.trim();
  const host = readFileSync(join(folder, "mapped.bin")).subarray(0, 5).toString();
  writeFileSync(join(folder, "mapped.bin"), Buffer.concat([Buffer.from("hostw"), Buffer.alloc(4091)]));
  const read = (await booted.run(ROOTS[0]!, MAP('    print("allowed", bytes(m[0:5]))'))).output.trim();
  await booted.stop();
  record("mmap", { "a shared map written by the guest": written, "what the host then reads": JSON.stringify(host), "a shared map read after the host wrote": read });
  expect(written).not.toBe("");
});

// A shared map (mmap MAP_SHARED) of a file in a Plan 9 share: on 9p under cache=none the guest refuses it with EINVAL
// where virtio-fs gives ENODEV (milestone B's stand-in showed EINVAL), so SQLite's WAL fails plainly either way.
// Decides the model's note about databases in a folder (Mw6).
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts mmap
// Written down, no PASS or FAIL: "refused" is the safe behaviour; "allowed" needs the note.

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
  await booted.stop();
  record("mmap", { "a shared map written by the guest": written, "what the host then reads": JSON.stringify(host) });
  expect(written).not.toBe("");
});

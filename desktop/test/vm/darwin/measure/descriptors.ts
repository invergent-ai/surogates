// Apple's virtio-fs server's limits, which Apple does not document, against a folder the size of a large
// node_modules: 150 000 files in 1 500 folders, made on the host, then found, read and put in git from the
// guest. Linux's virtiofsd held a descriptor for each file the guest kept, up to the user's hard limit.
// Decides a ceiling to write down.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts descriptors
// PASS or FAIL: all 150 000 found, a thousand read, git takes them, and no "Too many open files".

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { lab, ms, ready, record, ROOTS, scratch } from "./harness.js";

it("serves a 150 000-file folder to the guest", async () => {
  const folder = scratch("descriptors");
  for (let d = 0; d < 1_500; d += 1) {
    const at = join(folder, "node_modules", `package-${d}`);
    mkdirSync(at, { recursive: true });
    for (let f = 0; f < 100; f += 1) writeFileSync(join(at, `file-${f}.js`), `module.exports = ${d * 100 + f};\n`);
  }
  const booted = await lab("descriptors");
  const root = ROOTS[0]!;
  await ready(booted.guest, root, folder);
  let begun = performance.now();
  const found = (await booted.run(root, "find node_modules -type f | wc -l", 1_800)).output.trim();
  const findMs = ms(begun);
  const read = (await booted.run(root, "find node_modules -name 'file-7.js' | head -n 1000 | xargs cat | wc -l", 600)).output.trim();
  begun = performance.now();
  const git = await booted.run(root, "git init -q && git add -A 2>&1 | tail -n 3; git -c user.name=m -c user.email=m@surogate.invalid commit -qm all 2>&1 | tail -n 3; git status --porcelain | wc -l", 3_600);
  const gitMs = ms(begun);
  await booted.stop();
  const failed = record("descriptors", {
    "files found": found, "find, ms": findMs, "a thousand read, their lines": read, "git add, commit and status": git.output.trim(), "git, ms": gitMs,
  }, {
    "all 150 000 found": found === "150000",
    "a thousand read": read === "1000",
    "git committed them all, and has nothing left to add": git.output.trim().endsWith("0"),
    "no Too many open files": !/Too many open files|EMFILE/.test(`${git.output}${found}${read}`),
  });
  expect(failed).toEqual([]);
});

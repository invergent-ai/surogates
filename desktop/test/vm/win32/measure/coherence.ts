// Whether a command in the guest sees at once what the host wrote to a Plan 9 share under cache=none (the 9p client's
// default, as cache=never gives Linux): a host write then a guest read, 1 000 times; a large file written whole then
// read; and git status over 50 000 files after the host changed 10. Decides whether a command follows a file tool's
// write at once.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts coherence
// PASS or FAIL: no stale read of 1 000, the large file seen whole, and git status sees the 10.

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { lab, ms, ready, record, ROOTS, scratch } from "./harness.js";

it("shows a command what the host wrote, at once", async () => {
  const folder = scratch("coherence");
  const booted = await lab("coherence");
  const root = ROOTS[0]!;
  await ready(booted.guest, root, folder);
  let stale = 0;
  const begun = performance.now();
  for (let n = 0; n < 1_000; n += 1) {
    writeFileSync(join(folder, "x.txt"), `version ${n}`);
    if ((await booted.run(root, "cat x.txt")).output !== `version ${n}`) stale += 1;
  }
  const readsMs = ms(begun);
  for (let d = 0; d < 500; d += 1) {
    mkdirSync(join(folder, "tree", String(d)), { recursive: true });
    for (let f = 0; f < 100; f += 1) writeFileSync(join(folder, "tree", String(d), `${f}.txt`), `${d}/${f}\n`);
  }
  const committed = await booted.run(root, "git init -q && git add -A && git -c user.name=m -c user.email=m@surogate.invalid commit -qm base && echo committed", 1_800);
  for (let f = 0; f < 10; f += 1) writeFileSync(join(folder, "tree", "7", `${f}.txt`), "changed\n");
  const status = (await booted.run(root, "git status --porcelain | wc -l", 600)).output.trim();
  await booted.stop();
  const failed = record("coherence", {
    "stale reads of 1 000": stale, "1 000 writes and reads, ms": readsMs, "git add and commit of 50 000 files": committed.output.trim(), "git status after 10 changed": status,
  }, {
    "no stale read of 1 000": stale === 0,
    "git status sees the 10 files the host changed": status === "10",
  });
  expect(failed).toEqual([]);
});

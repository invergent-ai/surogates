// Whether a command in the guest sees at once what the host wrote to its folder, through Apple's virtio-fs
// server, whose caching the guest cannot set (Linux's virtiofsd runs cache=never): a host write then a
// guest read, 1 000 times; a large file written whole then read; and git status over 50 000 files after the
// host changed 10 of them, in a repository the Mac made: a session makes no .git in a shared folder, on any OS
// (vm/rule.bpf.c), and commits in one the user has. Decides whether the agent must drop the guest's caches before
// each command.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts coherence
// PASS, FAIL or UNTESTED: no stale read of 1 000 (Linux: 0), the large file seen whole, and git status sees the 10.

import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { judged, lab, ms, type Ran, ready, record, ROOTS, scratch } from "./harness.js";

it("shows a command what the host wrote, at once", async () => {
  const folder = scratch("coherence");
  // The repository, as the user's: made on the Mac.
  execFileSync("git", ["init", "-q", folder]);
  const booted = await lab("coherence");
  const root = ROOTS[0]!;
  await ready(booted.guest, root, folder);
  let stale = 0;
  const reads: Ran[] = [];
  const begun = performance.now();
  for (let n = 0; n < 1_000; n += 1) {
    writeFileSync(join(folder, "x.txt"), `version ${n}`);
    const read = await booted.run(root, "cat x.txt");
    reads.push(read);
    if (read.output !== `version ${n}`) stale += 1;
  }
  const readsMs = ms(begun);
  const lines = Array.from({ length: 3_000 }, (_, n) => `value_${n} = ${n}`);
  writeFileSync(join(folder, "large.py"), `${lines.join("\n")}\n`);
  const whole = await booted.run(root, "wc -l < large.py; tail -n 1 large.py");
  const large = whole.output.trim();
  for (let d = 0; d < 500; d += 1) {
    mkdirSync(join(folder, "tree", String(d)), { recursive: true });
    for (let f = 0; f < 100; f += 1) writeFileSync(join(folder, "tree", String(d), `${f}.txt`), `${d}/${f}\n`);
  }
  let at = performance.now();
  const committed = await booted.run(root, "git add -A && git -c user.name=m -c user.email=m@surogate.invalid commit -qm base && echo committed", 1_800);
  const commitMs = ms(at);
  for (let f = 0; f < 10; f += 1) writeFileSync(join(folder, "tree", "7", `${f}.txt`), "changed\n");
  at = performance.now();
  const changed = await booted.run(root, "git status --porcelain | wc -l", 600);
  const status = changed.output.trim();
  const statusMs = ms(at);
  await booted.stop();
  const failed = record("coherence", {
    "stale reads of 1 000": stale, "1 000 writes and reads, ms": readsMs, "the large file, its lines and last": large,
    "git add and commit of 50 000 files": `${committed.output.trim()} in ${commitMs} ms`, "git status after 10 changed": `${status} in ${statusMs} ms`,
  }, {
    "no stale read of 1 000": judged(reads, stale === 0),
    "the large file seen whole": judged([whole], large === `3000\nvalue_2999 = 2999`),
    "git status sees the 10 files the host changed": judged([committed, changed], committed.output.trim() === "committed" && status === "10"),
  });
  expect(failed).toEqual([]);
});

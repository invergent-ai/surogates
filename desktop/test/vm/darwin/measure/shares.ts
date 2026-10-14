// Folders shared into the running guest on Apple's framework, in each layout surogate-vm has: one device
// for every folder whose whole set of directories is replaced at each change (multiple), and eight
// devices of a folder each, set at a root's setup and emptied at its teardown (single). Apple documents
// VZVirtioFileSystemDevice.share as settable, and not whether a running guest takes a new one. For each:
// how long a share and its removal take, whether each root reads its own folder after others come and go,
// and what a root still holding a folder taken out sees, and whether it ever sees another's. Decides the
// layout (src/vm/darwin.ts, LAYOUT).
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts shares
// PASS or FAIL, for each layout: every root reads its own folder; a root whose folder was taken out sees
// no other root's. Use the first layout that passes both; multiple is the one darwin.ts uses now.

import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import type { Share } from "../../../../src/guest/protocol.js";
import { lab, ms, ready, record, ROOTS, scratch, signal } from "./harness.js";

const background = (command: string) => ({ command, workdir: null, task_id: "held", pty: false, notify_on_complete: false, watcher_interval: null });

it("shares folders into the running guest and takes them out again, in each layout", async () => {
  const failures: string[] = [];
  for (const layout of ["multiple", "single"] as const) {
    const [a, b, c, d] = ["a", "b", "c", "d"].map((name) => {
      const folder = scratch(`shares-${layout}-${name}`);
      writeFileSync(join(folder, `${name}.txt`), name);
      return folder;
    }) as [string, string, string, string];
    const found: Record<string, unknown> = {};
    const reads: boolean[] = [];
    let unseen = true;
    try {
      const booted = await lab(`shares-${layout}`, { layout });
      const [one, two, three, four] = ROOTS as [string, string, string, string];
      const read = async (root: string, name: string) => reads.push((await booted.run(root, `cat ${name}.txt`)).output === name);
      let begun = performance.now();
      await ready(booted.guest, one, a);
      found["the first share and setup, ms"] = ms(begun);
      await read(one, "a");
      begun = performance.now();
      await ready(booted.guest, two, b);
      found["a second share and setup, ms"] = ms(begun);
      await read(two, "b");
      await read(one, "a");
      begun = performance.now();
      await booted.guest.teardown(one);
      found["a teardown and its share's removal, ms"] = ms(begun);
      await ready(booted.guest, three, c);
      await read(three, "c");
      await read(two, "b");
      // The second root's folder taken out under it while a process of its own sits in it.
      await booted.guest.op(two, "start", background("sleep 600"), signal());
      const second: Share = layout === "multiple" ? { kind: "vz", tag: "surogate", name: "r2" } : { kind: "vz", tag: "s2" };
      begun = performance.now();
      found["the held folder's removal"] = await booted.vm.unshare(second, performance.now() + 15_000).then(() => `let go in ${ms(begun)} ms`, (error: Error) => `refused: ${error.message}`);
      found["what the root sees of its folder taken out"] = (await booted.run(two, "ls -la; cat b.txt", 20)).output;
      // A fourth folder, which in the single layout takes the second's device.
      await ready(booted.guest, four, d);
      await read(four, "d");
      const after = (await booted.run(two, "ls -la", 20)).output;
      found["what it sees once another folder is shared"] = after;
      unseen = !after.includes("d.txt");
      await booted.stop();
    } catch (error) {
      found.failed = (error as Error).message;
      reads.push(false);
    }
    failures.push(...record(`shares-${layout}`, found, {
      [`${layout}: every root reads its own folder`]: reads.length === 6 && reads.every(Boolean),
      [`${layout}: a root whose folder was taken out sees no other root's`]: unseen,
    }));
  }
  expect(failures).toEqual([]);
});

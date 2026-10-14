// Folders shared into the running guest on Apple's framework: eight devices of a folder each, set at a root's setup
// and emptied at its teardown (src/vm/darwin.ts). A running guest takes a device's new folder, and a mount of the
// device made before is served it too: so a device takes another folder only once the guest has released it, nothing
// of it mounted in any namespace any more (guest/root.ts, Connections), and one let go without that word holds its
// empty folder until the VM stops. How long a share and its removal take; whether each root reads its own folder as
// others come and go; that a root torn down gives its device to the next folder; and that a root still holding its
// device, whose folder was taken out under it, keeps that device from the next folder and never sees another root's.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts shares
// PASS, FAIL or UNTESTED: every root reads its own folder; a device is given again once its root is torn down; a root
// whose folder was taken out keeps its device from the next folder, and sees no other root's.

import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import type { Share } from "../../../../src/guest/protocol.js";
import { judged, lab, ms, type Ran, ready, record, ROOTS, scratch, signal } from "./harness.js";

const background = (command: string) => ({ command, workdir: null, task_id: "held", pty: false, notify_on_complete: false, watcher_interval: null });

it("shares folders into the running guest on its eight devices, and gives a device another only once no root holds it", async () => {
  const [a, b, c, d] = ["a", "b", "c", "d"].map((name) => {
    const folder = scratch(`shares-${name}`);
    writeFileSync(join(folder, `${name}.txt`), name);
    return folder;
  }) as [string, string, string, string];
  const found: Record<string, unknown> = {};
  const reads: Ran[] = [];
  const read = (run: Ran, name: string) => {
    reads.push(run);
    return run.output === name;
  };
  // The device each folder was shared on, and whether each was let go as released.
  const tags = new Map<string, string>();
  const letGo: Array<[string, boolean]> = [];
  let readsOwn = false;
  let given: boolean | null = null;
  let kept: boolean | null = null;
  let seen: Ran[] = [];
  try {
    const booted = await lab("shares");
    const { vm } = booted;
    const share = vm.share.bind(vm);
    const unshare = vm.unshare.bind(vm);
    vm.share = async (folder, ...rest) => {
      const made = await share(folder, ...rest);
      tags.set(folder, made.tag);
      return made;
    };
    vm.unshare = async (made, deadline, as) => {
      letGo.push([made.tag, as?.released === true]);
      return unshare(made, deadline, as);
    };
    const [one, two, three, four] = ROOTS as [string, string, string, string];
    const cat = async (root: string, name: string) => read(await booted.run(root, `cat ${name}.txt`), name);
    let begun = performance.now();
    await ready(booted.guest, one, a);
    found["the first share and setup, ms"] = ms(begun);
    const own = [await cat(one, "a")];
    begun = performance.now();
    await ready(booted.guest, two, b);
    found["a second share and setup, ms"] = ms(begun);
    own.push(await cat(two, "b"), await cat(one, "a"));
    begun = performance.now();
    await booted.guest.teardown(one);
    found["a teardown and its share's removal, ms"] = ms(begun);
    // The first root is gone, and every mount of its device with it: the third folder may take that device.
    await ready(booted.guest, three, c);
    own.push(await cat(three, "c"), await cat(two, "b"));
    given = tags.get(c) === tags.get(a) && letGo.some(([tag, released]) => tag === tags.get(a) && released);
    // The second root's folder taken out under it while a process of its own sits in it: no teardown says it released.
    await booted.guest.op(two, "start", background("sleep 600"), signal());
    const second: Share = { kind: "vz", tag: tags.get(b)! };
    begun = performance.now();
    found["the held folder's removal"] = await vm.unshare(second, performance.now() + 15_000).then(() => `let go in ${ms(begun)} ms`, (error: Error) => `refused: ${error.message}`);
    const taken = await booted.run(two, "ls -la; cat b.txt", 20);
    found["what the root sees of its folder taken out"] = taken.output;
    // A fourth folder: never on the second's device, which the second root may still hold.
    await ready(booted.guest, four, d);
    own.push(await cat(four, "d"));
    const after = await booted.run(two, "ls -la", 20);
    found["what it sees once another folder is shared"] = after.output;
    seen = [taken, after];
    kept = tags.get(d) !== tags.get(b) && !after.output.includes("d.txt");
    readsOwn = own.every(Boolean);
    await booted.stop();
  } catch (error) {
    found.failed = (error as Error).message;
  }
  found["the devices, by folder"] = Object.fromEntries([a, b, c, d].map((folder) => [folder.split("/").at(-1), tags.get(folder) ?? "none"]));
  found["each let go, and whether released"] = letGo.map(([tag, released]) => `${tag} ${released ? "released" : "not released"}`);
  const failed = record("shares", found, {
    "every root reads its own folder": reads.length === 6 ? judged(reads, readsOwn) : false,
    "a device is given again once its root is torn down": given === true,
    "a root whose folder was taken out keeps its device from the next folder, and sees no other root's": kept === null ? false : judged(seen, kept),
  });
  expect(failed).toEqual([]);
});

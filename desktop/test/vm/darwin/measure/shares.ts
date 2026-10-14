// Folders shared into the running guest on Apple's framework: eight devices of a folder each, set at a root's setup
// and emptied at its teardown (src/vm/darwin.ts). A running guest takes a device's new folder, and a mount of the
// device made before is served it too: so a device takes another folder only once the guest has released it, nothing
// of it mounted in any namespace and no file of it open (guest/root.ts, Superblocks), and one let go without that word
// holds its empty folder until the VM stops. A device never used in this boot is given before one released.
// It measures how long a share and its removal take; whether each root reads its own folder as others come and go;
// that a root's share the guest's root holds open, as a root's process could, is not released at its teardown and its
// device takes no other folder; that a root whose folder was taken out from under it keeps its device from the next
// folder and never sees another root's; and that a root torn down gives its device to a folder once every device never
// used has one. With the measurements' agent disk, whose probe holds the share as the guest's root (prepare.sh).
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts shares
// PASS, FAIL or UNTESTED for each. A FAIL of either of the two in the middle is a security finding.

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import type { Share } from "../../../../src/guest/protocol.js";
import { judged, lab, ms, PROBING_DISK, type Ran, ready, record, ROOTS, scratch, signal, UNTESTED, type Verdict } from "./harness.js";

const background = (command: string) => ({ command, workdir: null, task_id: "held", pty: false, notify_on_complete: false, watcher_interval: null });
// Nine roots: as many as take every device once, and one more.
const ALL = [...ROOTS, "5b4a3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d", "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f", "8e9f0a1b-2c3d-4e5f-8a6b-7c8d9e0f1a2b",
  "4d5e6f7a-8b9c-4d0e-9f1a-2b3c4d5e6f7a", "6f7a8b9c-0d1e-4f2a-8b3c-4d5e6f7a8b9c"];
const NAMES = ["a", "b", "c", "d", "e", "f", "g", "h", "i"];
// The probe the guest's root runs in a root's folder (probe.sh): a file of the folder held open by a process of its
// own, as a root's command could hold one, while the root is torn down.
const HOLD = `sh -c 'exec 3< "$1"; exec sleep 600' sh "$here/b.txt" > /dev/null 2>&1 &\necho held $!\n`;

it("shares folders into the running guest on its eight devices, and gives a device another only once nothing of it is left", async () => {
  const folders = NAMES.map((name) => {
    const folder = scratch(`shares-${name}`);
    writeFileSync(join(folder, `${name}.txt`), name);
    return folder;
  });
  const found: Record<string, unknown> = {};
  const reads: Ran[] = [];
  const own: boolean[] = [];
  // The device each folder was shared on, and each let go with whether it was released.
  const tags = new Map<string, string>();
  const letGo: Array<[string, boolean]> = [];
  const tagOf = (name: string) => tags.get(folders[NAMES.indexOf(name)]!);
  let held: Verdict = UNTESTED;
  let kept: Verdict = UNTESTED;
  let given: Verdict = UNTESTED;
  try {
    const booted = await lab("shares", { agentDisk: PROBING_DISK });
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
    const root = (name: string) => ALL[NAMES.indexOf(name)]!;
    const setUp = (name: string) => ready(booted.guest, root(name), folders[NAMES.indexOf(name)]!);
    const cat = async (name: string) => {
      const run = await booted.run(root(name), `cat ${name}.txt`);
      reads.push(run);
      own.push(run.output === name);
    };
    let begun = performance.now();
    await setUp("a");
    found["the first share and setup, ms"] = ms(begun);
    await cat("a");
    begun = performance.now();
    await setUp("b");
    found["a second share and setup, ms"] = ms(begun);
    await setUp("c");
    await cat("b");
    await cat("c");
    await cat("a");

    // The second root's share held open by the guest's root, then the root torn down: not released.
    const probe = join(folders[1]!, ".measure-probe");
    writeFileSync(probe, `here=$(dirname "$0")\n${HOLD}`);
    for (const end = Date.now() + 30_000; Date.now() < end && !existsSync(`${probe}.out`); await new Promise((resolve) => setTimeout(resolve, 200)));
    const holding = existsSync(`${probe}.out`) ? readFileSync(`${probe}.out`, "utf8").trim() : "the probe did not run: is the measurements' agent disk made (prepare.sh)?";
    found["the guest's root holding the second root's share"] = holding;
    begun = performance.now();
    await booted.guest.teardown(root("b"));
    found["a teardown of a root whose share is held, ms"] = ms(begun);

    // The first root torn down with nothing of its share left: released.
    begun = performance.now();
    await booted.guest.teardown(root("a"));
    found["a teardown and its share's removal, ms"] = ms(begun);

    // The third root's folder taken out under it while a process of its own sits in it: no teardown says it released.
    await booted.guest.op(root("c"), "start", background("sleep 600"), signal());
    const third: Share = { kind: "vz", tag: tagOf("c")! };
    begun = performance.now();
    found["the held folder's removal"] = await vm.unshare(third, performance.now() + 15_000).then(() => `let go in ${ms(begun)} ms`, (error: Error) => `refused: ${error.message}`);
    const taken = await booted.run(root("c"), "ls -la; cat c.txt", 20);
    found["what the root sees of its folder taken out"] = taken.output;

    // Six folders more: every device never used, then the first's, released; never the second's or the third's.
    for (const name of ["d", "e", "f", "g", "h", "i"]) {
      await setUp(name);
      await cat(name);
    }
    const after = await booted.run(root("c"), "ls -la", 20);
    found["what it sees once the others are shared"] = after.output;
    const later = ["d", "e", "f", "g", "h", "i"].map(tagOf);
    const released = (tag: string | undefined) => letGo.find(([each]) => each === tag)?.[1];
    held = judged([{ ran: holding.startsWith("held ") }], released(tagOf("b")) === false && !later.includes(tagOf("b")));
    kept = judged([taken, after], !later.includes(tagOf("c")) && !NAMES.filter((name) => name !== "c").some((name) => after.output.includes(`${name}.txt`)));
    given = released(tagOf("a")) === true && later.join(" ") === `s4 s5 s6 s7 s8 ${tagOf("a")}`;
    await booted.stop();
  } catch (error) {
    found.failed = (error as Error).message;
  }
  found["the devices, by folder"] = Object.fromEntries(NAMES.map((name) => [name, tagOf(name) ?? "none"]));
  found["each let go, and whether released"] = letGo.map(([tag, released]) => `${tag} ${released ? "released" : "not released"}`);
  const failed = record("shares", found, {
    "every root reads its own folder": reads.length === 10 ? judged(reads, own.every(Boolean)) : UNTESTED,
    "a root's share held open is not released at its teardown, and its device takes no other folder": held,
    "a root whose folder was taken out keeps its device from the next folder, and sees no other root's": kept,
    "a root torn down gives its device to a folder once every device never used has one": given,
  });
  expect(failed).toEqual([]);
});

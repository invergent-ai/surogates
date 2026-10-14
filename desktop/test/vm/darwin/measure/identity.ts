// Whether Apple's share shows the guest the Mac's own inode numbers, for a later check of a root's folder by its
// identity as the guest sees it (VZSharedDirectory's init(url:readOnly:) takes a folder by its URL; what inode
// numbers the framework's server gives the guest, and when it opens the folder, Apple does not say). It records the
// inode of the share's folder, of a folder and a file in it, and of a hard link's two names, as a command of the
// root sees them and as the Mac does; then, once the Mac has moved the folder aside and made another at its path,
// as a command of a chat whose folder held it could, which of the two the root is shown, and its inodes.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts identity
// Prints what each side saw. PASS or FAIL: the root was set up and the look ran. Whether the inodes are the Mac's,
// and which folder the root is shown after the move, decide whether the VM manager can check a share's folder by
// its identity in the guest, and are not judged here.

import { linkSync, mkdirSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { lab, ready, record, ROOTS, scratch } from "./harness.js";

const NAMES = [".", "sub", "file.txt", "linked.txt"];

it("shows what inode numbers Apple's share gives the guest, and which folder a root is shown once another is put at its path", async () => {
  const folder = scratch("identity");
  const aside = `${folder}-aside`;
  rmSync(aside, { recursive: true, force: true });
  mkdirSync(join(folder, "sub"));
  writeFileSync(join(folder, "file.txt"), "the first folder\n");
  linkSync(join(folder, "file.txt"), join(folder, "linked.txt"));
  const found: Record<string, unknown> = { "the Mac's inodes": Object.fromEntries(NAMES.map((name) => [name, statSync(join(folder, name)).ino])) };
  let looked = false;
  const booted = await lab("identity");
  try {
    const root = ROOTS[0]!;
    await ready(booted.guest, root, folder);
    const look = await booted.run(root, `stat -c '%n %i' ${NAMES.join(" ")}`);
    found["the guest's inodes"] = look.output.trim().split("\n");
    looked = look.returncode === 0;
    renameSync(folder, aside);
    mkdirSync(folder);
    writeFileSync(join(folder, "file.txt"), "the second folder\n");
    found["the inodes of the folder made at its path, on the Mac"] = { ".": statSync(folder).ino, "file.txt": statSync(join(folder, "file.txt")).ino };
    found["what the root is shown once the folder was moved aside and another made at its path"] =
      (await booted.run(root, "cat file.txt; stat -c '%n %i' . file.txt")).output.trim().split("\n");
  } catch (error) {
    found.failed = (error as Error).message;
    looked = false;
  } finally {
    await booted.stop();
    rmSync(aside, { recursive: true, force: true });
  }
  expect(record("identity", found, { "the root was set up and the look ran": looked })).toEqual([]);
});

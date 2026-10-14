// WHOSE RIGHTS HCS'S PLAN 9 SERVER SERVES A FOLDER WITH: the largest unknown of the Windows VM, and its first security stop.
// A share is added with the caller's own token and UseShareRootIdentity (native/windows/hcs, flags); the guest must
// reach exactly what the user can, and nothing more. On a standard account and on an administrator's: a folder with a
// subfolder only an administrator can read, another user's folder, and a file the user may read but not write. A
// server that serves a share with the VM's or SYSTEM's rights would let a user read what they cannot, and is a
// security finding: folder sharing stays off, and the app's own Plan 9 server is built instead.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts identity
// FAIL is a security finding: STOP and report it before anything else.

import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { lab, ready, record, ROOTS, scratch } from "./harness.js";

// icacls grants, run on the host before the share: a subfolder only administrators may read, and a file read-only to
// the user. Run this measurement elevated for the admin-readable subfolder; on a standard account, note it was set by
// an administrator beforehand (the test day's step).
const guard = (path: string, grant: string) => {
  try {
    execFileSync("icacls.exe", [path, "/inheritance:r", "/grant:r", grant], { encoding: "utf8" });
    return "set";
  } catch (error) {
    return `not set: ${(error as Error).message}`;
  }
};

it("shares a folder with the user's own rights, and nothing more", async () => {
  const folder = scratch("identity");
  writeFileSync(join(folder, "mine.txt"), "mine\n");
  const secret = join(folder, "admin-only");
  mkdirSync(secret, { recursive: true });
  writeFileSync(join(secret, "secret.txt"), "secret\n");
  const guarded = guard(secret, "Administrators:(OI)(CI)F");
  const readonly = join(folder, "read-only.txt");
  writeFileSync(readonly, "ro\n");
  const readonlyGuard = guard(readonly, `${process.env.USERNAME ?? "user"}:R`);
  const other = process.env.SUROGATE_OTHER_USER_FOLDER;

  const booted = await lab("identity");
  const root = ROOTS[0]!;
  await ready(booted.guest, root, folder);
  const mine = (await booted.run(root, "cat mine.txt 2>&1")).output.trim();
  const subfolder = (await booted.run(root, "ls admin-only 2>&1 && cat admin-only/secret.txt 2>&1 || echo refused")).output.trim();
  const write = (await booted.run(root, "echo x >> read-only.txt 2>&1 && echo wrote || echo refused")).output.trim();
  let outside = "not run: set SUROGATE_OTHER_USER_FOLDER to another user's folder";
  if (other) {
    const forced = await booted.vm.share(other, 1000, performance.now() + 15_000).then(() => "the service shared it", (error: Error) => `refused at the service: ${error.message}`);
    outside = forced;
  }
  await booted.stop();
  const failed = record("identity", {
    "the admin-only subfolder's guard": guarded, "the read-only file's guard": readonlyGuard,
    "the user's own file, read in the guest": mine, "the admin-only subfolder, read in the guest": subfolder,
    "a write to the read-only file, in the guest": write, "another user's folder": outside,
  }, {
    "the guest reads the user's own file": mine === "mine",
    "the guest CANNOT read a subfolder only an administrator can (security)": /refused|denied|not permitted/i.test(subfolder) && !subfolder.includes("secret"),
    "the guest CANNOT write a file the user may only read (security)": /refused|denied|not permitted/i.test(write),
    "another user's folder is refused, or unreadable in the guest (security)": !other || /refused/i.test(outside),
  });
  expect(failed).toEqual([]);
});

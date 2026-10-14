// Folders of every kind a Windows PC has, read through the share and the file tools: Documents moved into
// OneDrive with cloud-only files; a USB exFAT drive; a ReFS Dev Drive (its identity); a subst drive (by its real
// path); a network drive (refused at binding by the file host's folder binding); a folder inside a compressed or
// EFS-encrypted folder. Decides the binding's words. Each path comes from an environment variable the tester sets to
// a folder of that kind; a network drive the tester sets to a mapped drive, which the binding must refuse.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts folders
// Written down: what the guest reads in each, and each folder's identity. A network drive's binding is owed to D.

import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { folderOf, lab, record, ROOTS } from "./harness.js";

const KINDS = [
  ["OneDrive Documents", "SUROGATE_ONEDRIVE_FOLDER"],
  ["exFAT USB", "SUROGATE_EXFAT_FOLDER"],
  ["ReFS Dev Drive", "SUROGATE_REFS_FOLDER"],
  ["subst drive", "SUROGATE_SUBST_FOLDER"],
  ["compressed or encrypted", "SUROGATE_COMPRESSED_FOLDER"],
] as const;

it("reads each kind of folder through the share", async () => {
  const found: Record<string, unknown> = {};
  const booted = await lab("folders");
  for (const [n, [kind, variable]] of KINDS.entries()) {
    const folder = process.env[variable];
    if (!folder) {
      found[kind] = `skipped: set ${variable}`;
      continue;
    }
    try {
      const identity = folderOf(folder);
      found[`${kind}: identity`] = { dev: identity.dev, ino: identity.ino };
      writeFileSync(join(folder, ".surogate-folders-probe"), "host\n");
      const answer = await booted.guest.ready(ROOTS[n]!, identity);
      found[`${kind}: the guest`] = answer ? JSON.stringify(answer) : (await booted.run(ROOTS[n]!, "ls -la; cat .surogate-folders-probe 2>&1; stat -f -c %T .")).output.trim();
    } catch (error) {
      found[`${kind}: error`] = (error as Error).message;
    }
  }
  await booted.stop();
  record("folders", found);
  expect(Object.keys(found).length).toBeGreaterThan(0);
});

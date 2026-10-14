// Owners, names and links on HCS's Plan 9 share, with uid=<n>;gid=<n> in the aname (native/windows/hcs, with and
// without LinuxMetadata): the uid, gid and mode the guest sees; who owns, on the host (NTFS), a file the guest makes;
// chmod, symbolic and hard links, FIFOs, mknod, xattrs and a rename over, by a root; a write by another guest uid.
// AND THE SECURITY CHECK: a junction and a symbolic link in the folder that lead outside it — what the guest sees,
// and whether a read through them reaches outside the folder. Also the names NTFS resolves to another: ASCII case,
// ſ, ı, İ, the Kelvin sign, a trailing dot, a trailing space, a colon, NUL, CON.txt and a short name (GIT~1), and
// WSL's U+F000 mapping. Decides whether the share takes each root's uid from its aname, or shows each file as the asking uid's,
// as Apple's does, the fold's comment, and the owner's names finding.
//   npx vitest run -c test/vm/win32/measure/vitest.config.ts owners
// A read through a junction or symlink that reaches outside the folder is a SECURITY finding: STOP and report it.

import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { folded } from "../../../../src/files/protect.js";
import { lab, PROBING_DISK, ready, record, ROOTS, scratch } from "./harness.js";

// Names a volume Windows 11 formatted may resolve to another, each ASCII name and the others to look it up by.
const ALIASES: Array<[string, string[]]> = [
  ["hooks", ["HOOKS", "Hooks", "hooKs"]],
  [".git", [".gıt", ".gİt", ".GIT"]],
  ["kelvin", ["Åelvin"]],
];

it("shows the folder's files by the share's uid, and keeps links from reaching outside", async () => {
  const outside = scratch("owners-outside");
  writeFileSync(join(outside, "secret.txt"), "outside\n");
  const folder = scratch("owners");
  writeFileSync(join(folder, "host.txt"), "host\n");
  for (const [name] of ALIASES) writeFileSync(join(folder, name === "kelvin" ? "kelvin" : name), "");
  // A junction and a symbolic link in the folder leading outside it (mklink, where the account allows it).
  const links: Record<string, string> = {};
  for (const [flag, name] of [["/J", "junction"], ["/D", "symlink"]] as const) {
    try {
      execFileSync("cmd.exe", ["/c", "mklink", flag, join(folder, name), outside], { encoding: "utf8" });
      links[name] = "made";
    } catch (error) {
      links[name] = `not made: ${(error as Error).message}`;
    }
  }
  const booted = await lab("owners", { agentDisk: PROBING_DISK });
  const root = ROOTS[0]!;
  await ready(booted.guest, root, folder);
  const seen = (await booted.run(root, "id -u; id -g; stat -c '%u %g %a' . host.txt; stat -f -c %T .")).output.trim().split("\n");
  const tried = (await booted.run(root, [
    "t() { \"$@\" 2>&1 >/dev/null && echo ok || echo failed; }",
    "echo made: $(t sh -c 'echo guest > made.txt')", "echo chmod: $(t chmod 600 made.txt)", "echo hardlink: $(t ln made.txt hard.txt)",
    "echo symlink: $(t ln -s made.txt soft.txt)", "echo fifo: $(t mkfifo fifo)",
    "echo xattr: $(t python3 -c \"import os; os.setxattr('made.txt','user.surogate',b'1')\")", "echo renameover: $(t sh -c 'echo o>o.txt && mv -f o.txt made.txt')",
    ...ALIASES.flatMap(([, others]) => others.map((other) => `echo "lookup ${other}: $(t test -e '${other}')"`)),
    "for odd in '.git.' '.git ' 'CON.txt' 'GIT~1' 'a:b' 'x\\0y'; do echo \"make $odd: $(t sh -c \"touch '$odd'\")\"; done",
  ].join("\n"))).output;
  // The security read: through the junction and the symlink, does the guest reach the outside folder's file?
  const escape = (await booted.run(root, "for l in junction symlink; do echo \"$l: $(cat $l/secret.txt 2>&1 || echo refused)\"; done")).output.trim();
  await booted.stop();
  const misses: string[] = [];
  for (const [name, others] of ALIASES) for (const other of others) if (folded(other) !== folded(name === "kelvin" ? "kelvin" : name)) misses.push(`${other} vs ${name}`);
  const failed = record("owners", {
    "links in the folder": links, "the session's uid and gid, the folder's and host.txt's owner/group/mode, the fs": seen,
    "what a session could do": tried.trim().split("\n"), "a read through the junction and the symlink": escape, "names the fold and NTFS disagree on": misses,
  }, {
    "the folder shows as the root's own guest uid": seen[2]?.split(" ")[0] === seen[0],
    "a read through a junction or symlink does NOT reach outside the folder (security)": !/outside/.test(escape),
  });
  expect(failed).toEqual([]);
});

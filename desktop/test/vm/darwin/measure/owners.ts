// Owners, modes and names on Apple's share, which maps no owner: the uid, gid and mode the guest sees of
// the host's files; who owns a file the guest makes, on the host; chmod, hard and symbolic links, FIFOs,
// device nodes, extended attributes and a rename over a file, by a root; a write by another guest uid
// than the files', as the guest's root runs it (the measurements' agent disk, prepare.sh); and the names
// APFS takes as one, on the host and through the share: ASCII case, ſ, ı, İ, the Kelvin sign, ß, the
// ligatures, NFC and NFD. Checks that a root runs on its own uid there, which its folder shows it (guest/root.ts,
// checkOwner), and the protected-names fold (src/files/protect.ts, folded; vm/rule-match.h, sg_fold) against what
// APFS takes as one name. Another uid's write, with the share's owner shown as each asking uid's, is recorded alone:
// a root's share is mounted in no other root's namespace.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts owners
// PASS, FAIL or UNTESTED: a session runs as a uid from 10000 up and sees its folder as that uid's, the host user owns
// what the guest makes, and every pair of names APFS takes as one the fold takes as one too.

import { existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { folded } from "../../../../src/files/protect.js";
import { judged, lab, PROBING_DISK, ready, record, ROOTS, scratch, USER } from "./harness.js";

// Names APFS may take as one: each ASCII name, and the others to look it up by.
const ALIASES: Array<[string, string[]]> = [
  ["hooks", ["HOOKS", "Hooks", "hookſ", "hooKs", "hooKs"]],
  [".git", [".gıt", ".gİt", ".GIT"]],
  ["config", ["conﬁg", "CONFIG"]],
  ["dist-packages", ["diﬆ-packages", "diﬅ-packages", "diſt-packageſ"]],
  ["strasse", ["straße", "straẞe"]],
  ["café", ["café", "CAFÉ"]],
  ["staff", ["staﬀ"]],
];

const probe = async (folder: string, script: string, ms = 30_000) => {
  writeFileSync(join(folder, ".measure-probe"), `here=$(dirname "$0")\n${script}\n`);
  for (const end = Date.now() + ms; Date.now() < end; await new Promise((resolve) => setTimeout(resolve, 200))) {
    if (existsSync(join(folder, ".measure-probe.out"))) return readFileSync(join(folder, ".measure-probe.out"), "utf8");
  }
  return "the probe did not run: is the measurements' agent disk made (prepare.sh)?";
};

it("shows the host user's files as theirs, and takes the names APFS takes as one as one", async () => {
  // On the host first: which names APFS takes as one, in a folder of its own for each.
  const host: Record<string, string[]> = {};
  const misses: string[] = [];
  for (const [name, others] of ALIASES) {
    const folder = scratch(`owners-names-${name.replace(/\W/g, "")}`);
    writeFileSync(join(folder, name), "");
    host[name] = others.filter((other) => existsSync(join(folder, other)));
    for (const other of host[name]!) if (folded(other) !== folded(name)) misses.push(`${other} is ${name} to APFS, and not to the fold`);
  }
  const folder = scratch("owners");
  writeFileSync(join(folder, "host.txt"), "host\n", { mode: 0o644 });
  for (const [name] of ALIASES) writeFileSync(join(folder, name), "");
  const booted = await lab("owners", { agentDisk: PROBING_DISK });
  await ready(booted.guest, ROOTS[0]!, folder);
  const root = ROOTS[0]!;
  const looked = await booted.run(root, "id -u; id -g; stat -c '%u %g %a' . host.txt");
  const seen = looked.output.trim().split("\n");
  const trying = await booted.run(root, [
    "t() { \"$@\" 2>&1 && echo ok || echo failed; }",
    "echo made: $(t sh -c 'echo guest > made.txt')", "echo chmod: $(t chmod 600 made.txt)", "echo hard link: $(t ln made.txt hard.txt)",
    "echo symlink: $(t ln -s made.txt soft.txt)", "echo fifo: $(t mkfifo fifo)", "echo xattr: $(t python3 -c \"import os; os.setxattr('made.txt', 'user.surogate', b'1')\")",
    "echo rename over: $(t sh -c 'echo other > other.txt && mv -f other.txt made.txt')",
    ...ALIASES.flatMap(([, others]) => others.map((other) => `echo "lookup ${other}: $(t test -e '${other}')"`)),
  ].join("\n"));
  const tried = trying.output;
  const made = existsSync(join(folder, "made.txt")) ? statSync(join(folder, "made.txt")) : null;
  const asRoot = await probe(join(folder), [
    "echo device node: $(mknod \"$here/node\" c 1 3 2>&1 && echo ok || echo failed)",
    "echo another uid writes 0644: $(setpriv --reuid=4321 --regid=4321 --clear-groups sh -c \"echo x >> $here/host.txt\" 2>&1 && echo ok || echo failed)",
    "echo root chown: $(chown 4321:4321 \"$here/made.txt\" 2>&1 && echo ok || echo failed); stat -c 'after chown %u %g' \"$here/made.txt\"",
  ].join("\n"));
  await booted.stop();
  const failed = record("owners", {
    "names APFS takes as one, on the host": host, "the session's uid and gid, then the folder's and host.txt's owner, group and mode": seen,
    "what a session could do": tried.trim().split("\n"), "made.txt on the host": made ? `uid ${made.uid} gid ${made.gid} mode ${(made.mode & 0o777).toString(8)}` : "not made",
    "what the guest's root could do": asRoot.trim().split("\n"), "names APFS takes as one that the fold does not": misses,
  }, {
    "a session runs as a uid from 10000 up": judged([looked], Number(seen[0]) >= 10_000),
    "the session sees its folder and the Mac's file as its own uid's": judged([looked], [seen[2], seen[3]].every((line) => line?.split(" ")[0] === seen[0])),
    "the host user owns a file the guest makes": judged([trying], made?.uid === USER.uid),
    "every pair of names APFS takes as one the fold takes as one": misses.length === 0,
  });
  expect(failed).toEqual([]);
});

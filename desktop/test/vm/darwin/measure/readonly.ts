// The read-only share: a folder's place (src/vm/manager.ts, Guest.place) shares the folder itself to the
// guest's root readOnly (VZSharedDirectory(url:readOnly: true)), in place of the bwrap Linux serves it from.
// The guest's root, as the measurements' agent disk runs it (prepare.sh), tries everything that writes:
// a remount read-write, a write, an overwrite, chmod, a change of times, setxattr, truncate, fallocate, hard
// and symbolic links, a rename, an unlink and a mkdir, on the place's mount and on the device's own directory.
// Then, on the device's own mount, where every folder shared is a directory of one server's (darwin.ts, LAYOUT
// multiple), what one request of the guest's names across two of them: a rename and a hard link into the
// read-only folder from a read-write one, and out of it, a write through a link out, and an exchange
// (renameat2's RENAME_EXCHANGE) with the read-only side named first and second. Whether Apple's server holds
// readOnly for both of a request's folders it does not say. Decides that a place's folder stays safe from the
// guest's root on the one device; if any of these lands, the place's folder needs a device of its own.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts readonly
// PASS or FAIL: every write refused, and the folder on the host as it was.

import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { folderOf, lab, PROBING_DISK, record, ROOTS, scratch } from "./harness.js";

const KEY = "0123456789abcdef";
const ATTEMPTS = [
  ["write", 'sh -c "echo x > $at/made"'], ["overwrite", 'sh -c "echo x > $at/real.txt"'], ["chmod", "chmod 777 $at/real.txt"],
  ["setxattr", "python3 -c \"import os, sys; os.setxattr(sys.argv[1], 'user.x', b'1')\" $at/real.txt"], ["truncate", "truncate -s 0 $at/real.txt"],
  ["change of times", "touch -d 2001-01-01 $at/real.txt"], ["fallocate", "fallocate -l 1M $at/real.txt"],
  ["hard link", "ln $at/real.txt $at/linked"], ["symbolic link", "ln -s /etc/passwd $at/planted"], ["rename", "mv $at/real.txt $at/moved"],
  ["unlink", "rm $at/real.txt"], ["mkdir", "mkdir $at/made-dir"],
];
// One request across two of the device's folders, as the guest's root: os.rename, os.link, or renameat2 with
// RENAME_EXCHANGE through libc. Says nothing when it is done, and the error's name when it is refused.
const ACROSS = `import ctypes, errno, os, sys
what, a, b = sys.argv[1:4]
try:
    if what == "rename": os.rename(a, b)
    elif what == "link": os.link(a, b)
    else:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.renameat2(ctypes.c_int(-100), a.encode(), ctypes.c_int(-100), b.encode(), ctypes.c_uint(2)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
except OSError as error:
    print(errno.errorcode.get(error.errno, error.errno), end=" ")
    sys.exit(1)
`;
// The device's own mount, its read-only folder (the place's, shared second: its history is first), and the read-write
// one the root's folder is, found by a file the Mac left in it.
const DEVICE = "/run/surogate/shares/surogate";
const ACROSS_ATTEMPTS = [
  ["rename into the read-only folder", 'across rename "$rw/in.txt" "$ro/in.txt"'],
  ["rename out of the read-only folder", 'across rename "$ro/real.txt" "$rw/out.txt"'],
  ["hard link into the read-only folder", 'across link "$rw/in.txt" "$ro/linked-in.txt"'],
  ["hard link out of the read-only folder", 'across link "$ro/real.txt" "$rw/linked-out.txt"'],
  ["write through a link out", '[ -e "$rw/linked-out.txt" ] || { printf "no link "; false; } && sh -c \'echo x >> "$1"\' sh "$rw/linked-out.txt"'],
  ["exchange, the read-only side first", 'across exchange "$ro/real.txt" "$rw/swap.txt"'],
  ["exchange, the read-only side second", 'across exchange "$rw/swap.txt" "$ro/real.txt"'],
];
const seen = (real: string) => {
  const file = statSync(join(real, "real.txt"));
  return {
    names: readdirSync(real).sort(), text: readFileSync(join(real, "real.txt"), "utf8"),
    mode: file.mode, ino: file.ino, links: file.nlink, size: file.size, modified: file.mtimeMs,
  };
};

it("keeps a place's folder read-only to the guest's root, however it remounts, and across the device's folders", async () => {
  const real = scratch("readonly");
  writeFileSync(join(real, "real.txt"), "real\n", { mode: 0o644 });
  const before = seen(real);
  const booted = await lab("readonly", { agentDisk: PROBING_DISK });
  const history = join(booted.data, "history", KEY);
  mkdirSync(history, { recursive: true, mode: 0o700 });
  const { dev, ino } = statSync(real);
  const placed = await booted.guest.place({ key: KEY, history, real: { path: real, dev, ino } });
  if (placed) {
    await booted.stop();
    expect(record("readonly", { "the place": placed }, { "the place was mounted": false })).toEqual([]);
    return;
  }
  // A root of its own, so the guest has a share the probe's watcher looks in: the history's, where the probe is left.
  // Set up, or refused for the owner its folder shows: the agent mounts the share before it reads that.
  const writable = scratch("readonly-root");
  writeFileSync(join(writable, ".writable-side"), "");
  await booted.guest.ready(ROOTS[0]!, folderOf(writable));
  const script = [
    `for at in /run/surogate/places/${KEY}/real ${DEVICE}/r2; do`,
    '  echo "at $at"; echo "remount: $(mount -o remount,rw "$at" 2>&1 && echo allowed || echo refused)"',
    ...ATTEMPTS.map(([what, command]) => `  echo "${what}: $(${command} 2>&1 && echo allowed || echo refused)"`),
    "done",
    `cat > /tmp/across.py <<'EOF'\n${ACROSS}EOF`,
    'across() { python3 /tmp/across.py "$@"; }',
    `ro=${DEVICE}/r2; rw=$(for d in ${DEVICE}/r*; do [ -e "$d/.writable-side" ] && echo "$d"; done | head -n 1)`,
    'echo "at $ro, from $rw"',
    'echo x > "$rw/in.txt"; echo x > "$rw/swap.txt"',
    ...ACROSS_ATTEMPTS.map(([what, command]) => `echo "${what}: $(${command} 2>&1 && echo allowed || echo refused)"`),
  ].join("\n");
  writeFileSync(join(history, ".measure-probe"), `${script}\n`);
  let said = "";
  for (const end = Date.now() + 30_000; Date.now() < end && !said; await new Promise((resolve) => setTimeout(resolve, 200))) {
    try {
      said = readFileSync(join(history, ".measure-probe.out"), "utf8");
    } catch {
      // Not yet.
    }
  }
  await booted.stop();
  const after = seen(real);
  const lines = said.trim().split("\n");
  const failed = record("readonly", { "what the guest's root tried": lines, "the folder after": after }, {
    "the probe ran": lines.some((line) => line.startsWith("at ")),
    "the probe found the root's read-write folder on the device": lines.some((line) => /^at \S+, from \/run\/surogate\/shares\/surogate\/r\d+$/.test(line)),
    "every write refused": lines.filter((line) => !line.startsWith("at ") && !line.startsWith("remount")).every((line) => line.endsWith("refused")),
    "the folder on the host as it was": JSON.stringify(after) === JSON.stringify(before),
  });
  expect(failed).toEqual([]);
});

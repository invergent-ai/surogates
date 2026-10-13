import { readFileSync } from "node:fs";

import { expect, it } from "vitest";

const vm = (name: string) => readFileSync(new URL(`../vm/${name}`, import.meta.url), "utf8");

// The guest's init stops the boot unless every program of the rule attached, so it must count them all.
it("expects at boot as many attached hooks as the guest rule has lsm programs", () => {
  const programs = vm("rule.bpf.c").match(/^SEC\("lsm\//gm) ?? [];
  expect(Number(/^want=(\d+)/m.exec(vm("init"))?.[1])).toBe(programs.length);
});

// The rule judges a session by its uid alone, from 10000 up, on every OS: the reviewed rule of Linux's, with no map,
// no word of the kernel's command line and no file the agent reads that could add another uid to what it judges.
it("judges sessions from uid 10000 up alone, with nothing that adds a uid to them", () => {
  const rule = vm("rule.bpf.c");
  expect(rule).toMatch(/^#define FIRST_UID 10000u /m);
  expect(rule).toContain("\tif (uid < FIRST_UID || !dentry || !share_sb(sb))\n\t\treturn 0;");
  expect(rule).not.toContain('SEC(".maps")');
  const init = vm("init");
  expect(init).toContain("/usr/sbin/bpftool prog loadall /usr/lib/surogate/rule.bpf.o /sys/fs/bpf/surogate autoattach >&2");
  for (const gone of ["surogate.uid", "bpftool map", "pinmaps", "/run/surogate/scope"]) expect(init, gone).not.toContain(gone);
});

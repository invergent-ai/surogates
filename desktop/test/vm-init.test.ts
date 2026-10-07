import { readFileSync } from "node:fs";

import { expect, it } from "vitest";

const vm = (name: string) => readFileSync(new URL(`../vm/${name}`, import.meta.url), "utf8");

// The guest's init stops the boot unless every program of the rule attached, so it must count them all.
it("expects at boot as many attached hooks as the guest rule has lsm programs", () => {
  const programs = vm("rule.bpf.c").match(/^SEC\("lsm\//gm) ?? [];
  expect(Number(/^want=(\d+)/m.exec(vm("init"))?.[1])).toBe(programs.length);
});

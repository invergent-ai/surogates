import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";

import { expect, it } from "vitest";

const vm = (name: string) => readFileSync(new URL(`../vm/${name}`, import.meta.url), "utf8");

// The guest's init stops the boot unless every program of the rule attached, so it must count them all.
it("expects at boot as many attached hooks as the guest rule has lsm programs", () => {
  const programs = vm("rule.bpf.c").match(/^SEC\("lsm\//gm) ?? [];
  expect(Number(/^want=(\d+)/m.exec(vm("init"))?.[1])).toBe(programs.length);
});

// The rule's scope as init reads it from the kernel's command line: its function, run alone.
const scopeOf = (cmdline: string) => {
  const fn = /^scope_uid\(\) \{\n[\s\S]*?\n\}\n/m.exec(vm("init"))?.[0];
  const ran = spawnSync("/bin/sh", ["-euc", `${fn}\nscope_uid "$1"`, "sh", cmdline], { encoding: "utf8" });
  return ran.status === 0 ? { uid: ran.stdout.trim() } : { refused: ran.stdout.trim() };
};

it("judges the one uid the kernel's command line names as surogate.uid=, besides those from 10000 up, and none where it names none", () => {
  const linux = "root=/dev/vda ro console=hvc0 panic=-1 lsm=landlock,lockdown,yama,integrity,apparmor,bpf";
  expect(scopeOf(linux)).toEqual({ uid: "" });
  expect(scopeOf(`${linux} surogate.uid=501`)).toEqual({ uid: "501" });
  expect(scopeOf(`surogate.uid=1 ${linux}`)).toEqual({ uid: "1" });
  expect(scopeOf(`${linux} surogate.uid=4294967294`)).toEqual({ uid: "4294967294" });
  // A word that merely holds it is not it, and a glob is no file.
  expect(scopeOf(`${linux} xsurogate.uid=0 * /*`)).toEqual({ uid: "" });
});

it("refuses a scope no session may run as, and a second one, which fails the boot", () => {
  for (const value of ["0", "00", "0501", "-1", "501x", "", " ", "4294967295", "99999999999", "5e2", "+501"]) {
    expect(scopeOf(`console=hvc0 surogate.uid=${value}`), value).toEqual({ refused: `surogate.uid=${value.trim()} is not a uid a session may run as` });
  }
  expect(scopeOf("surogate.uid=501 surogate.uid=501")).toEqual({ refused: "the kernel's command line names surogate.uid= twice" });
  // The outcome the agent reads: a refused scope is a failed rule, and the scope it judges is what the agent runs sessions as.
  expect(vm("init")).toContain('elif ! scope=$(scope_uid "$(cat /proc/cmdline)"); then\n    echo "failed: $scope"');
  expect(vm("init")).toMatch(/map freeze pinned \/sys\/fs\/bpf\/surogate-maps\/scope && echo "\$scope" > \/run\/surogate\/scope/);
});

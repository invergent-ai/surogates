// The guest's walls on a Mac, as a session's command meets them: each root runs on a uid of its own from
// 10000 up, as on Linux, where its folder on Apple's share shows it that uid (guest/root.ts, checkOwner),
// and the protected-names rule judges those uids. A command sees no network interface but lo and reaches
// nothing past the proxy unasked; sees neither the host's ~/.ssh nor another root's folder or processes;
// runs as its root's own uid, not root's and not the other root's; and is refused a write to a protected
// name, and to one a case-insensitive volume takes as one.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts escape
// PASS or FAIL for each wall, or UNTESTED where a command it judges did not run in its root (verdicts.ts). A FAIL of
// either protected write is a security finding: stop and report it.

import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { judged, lab, type Ran, ready, record, ROOTS, scratch, signal, USER } from "./harness.js";

it("keeps a session's command within its walls", async () => {
  const [mine, theirs] = [scratch("escape-mine"), scratch("escape-theirs")];
  writeFileSync(join(theirs, "theirs.txt"), "theirs\n");
  // Made on the host, as git makes it: the write into it is the rule's to refuse.
  mkdirSync(join(mine, ".git", "hooks"), { recursive: true });
  const booted = await lab("escape");
  const [one, two] = ROOTS as [string, string];
  await ready(booted.guest, one, mine);
  await ready(booted.guest, two, theirs);
  await booted.guest.op(two, "start", { command: "sleep 300", workdir: null, task_id: "theirs", pty: false, notify_on_complete: false, watcher_interval: null }, signal());
  // Each command's output by what it looks at, and what it said whole: a check judges only what ran in its root.
  const runs: Record<string, Ran> = {};
  const look = async (what: string, command: string, root = one) => {
    runs[what] = await booted.run(root, command, 60);
    return runs[what].output.trim();
  };
  const found = {
    uid: await look("uid", "id -u"),
    "the other root's uid": await look("the other root's uid", "id -u", two),
    interfaces: await look("interfaces", "ip -o link | awk -F': ' '{print $2}' | tr '\\n' ' '"),
    "the host's ~/.ssh": await look("the host's ~/.ssh", `ls -d ${USER.home}/.ssh 2>&1 || true`),
    "the other root's folder": await look("the other root's folder", `ls ${theirs} 2>&1 || true`),
    "the other root's sleep": await look("the other root's sleep", "ps -eo args | grep -c '^sleep 300' || true"),
    "a fetch past the proxy unasked": await look("a fetch past the proxy unasked", "curl -s -m 10 -o /dev/null -w '%{http_code}' http://1.1.1.1/ || echo \" curl ended $?\""),
    ".git/hooks/pre-commit": await look(".git/hooks/pre-commit", "echo x > .git/hooks/pre-commit 2>&1 && echo written || echo refused"),
    // Through the alias, into .git/hooks itself where the volume takes hookſ as hooks: the rule's fold refuses it.
    ".git/hookſ/pre-commit": await look(".git/hookſ/pre-commit", "echo y > .git/hookſ/pre-commit 2>&1 && echo written || echo refused"),
    ".vscode/settings.json": await look(".vscode/settings.json", "mkdir .vscode 2>&1 && echo made || echo refused"),
  };
  await booted.stop();
  const of = (...whats: Array<keyof typeof found>) => whats.map((what) => runs[what]!);
  const failed = record("escape", found, {
    "a session runs as a uid of its root's own, from 10000 up": judged(of("uid", "the other root's uid"), Number(found.uid) >= 10_000 && found.uid !== found["the other root's uid"]),
    "no network interface but lo": judged(of("interfaces"), found.interfaces === "lo"),
    "no host ~/.ssh": judged(of("the host's ~/.ssh"), /No such file/.test(found["the host's ~/.ssh"])),
    "no other root's folder": judged(of("the other root's folder"), /No such file/.test(found["the other root's folder"])),
    "no other root's processes": judged(of("the other root's sleep"), found["the other root's sleep"] === "0"),
    "nothing past the proxy unasked": judged(of("a fetch past the proxy unasked"), !found["a fetch past the proxy unasked"].startsWith("2")),
    "a write to .git/hooks refused": judged(of(".git/hooks/pre-commit"), found[".git/hooks/pre-commit"].endsWith("refused")),
    "the folded .git/hookſ refused, and nothing reached .git/hooks": judged(of(".git/hookſ/pre-commit"),
      found[".git/hookſ/pre-commit"].endsWith("refused") && !existsSync(join(mine, ".git", "hooks", "pre-commit"))),
    "a protected folder refused": judged(of(".vscode/settings.json"), found[".vscode/settings.json"].endsWith("refused")),
  });
  expect(failed).toEqual([]);
});

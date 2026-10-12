// The guest's walls on a Mac, as a session's command meets them: every root runs as the host user's uid
// there (Apple's share maps no owner), so the protected-names rule's scope is that one uid, named on the
// kernel's command line (vm/init, guest/root.ts). A command sees no network interface but lo and reaches
// nothing past the proxy unasked; sees neither the host's ~/.ssh nor another root's folder or processes;
// runs as the host user's uid; and is refused a write to a protected name, and to one a case-insensitive
// volume takes as one.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts escape
// PASS or FAIL for each wall. A FAIL of either protected write is a security finding: stop and report it.

import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { lab, ready, record, ROOTS, scratch, signal, USER } from "./harness.js";

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
  const said = async (command: string) => (await booted.run(one, command, 60)).output.trim();
  const found = {
    uid: await said("id -u"),
    interfaces: await said("ip -o link | awk -F': ' '{print $2}' | tr '\\n' ' '"),
    "the host's ~/.ssh": await said(`ls -d ${USER.home}/.ssh 2>&1 || true`),
    "the other root's folder": await said(`ls ${theirs} 2>&1 || true`),
    "the other root's sleep": await said("ps -eo args | grep -c '^sleep 300' || true"),
    "a fetch past the proxy unasked": await said("curl -s -m 10 -o /dev/null -w '%{http_code}' http://1.1.1.1/ || echo \" curl ended $?\""),
    ".git/hooks/pre-commit": await said("echo x > .git/hooks/pre-commit 2>&1 && echo written || echo refused"),
    // Through the alias, into .git/hooks itself where the volume takes hookſ as hooks: the rule's fold refuses it.
    ".git/hookſ/pre-commit": await said("echo y > .git/hookſ/pre-commit 2>&1 && echo written || echo refused"),
    ".vscode/settings.json": await said("mkdir .vscode 2>&1 && echo made || echo refused"),
  };
  await booted.stop();
  const failed = record("escape", found, {
    "a session runs as the host user's uid": found.uid === String(USER.uid),
    "no network interface but lo": found.interfaces === "lo",
    "no host ~/.ssh": /No such file/.test(found["the host's ~/.ssh"]),
    "no other root's folder": /No such file/.test(found["the other root's folder"]),
    "no other root's processes": found["the other root's sleep"] === "0",
    "nothing past the proxy unasked": !found["a fetch past the proxy unasked"].startsWith("2"),
    "a write to .git/hooks refused": found[".git/hooks/pre-commit"].endsWith("refused"),
    "the folded .git/hookſ refused, and nothing reached .git/hooks": found[".git/hookſ/pre-commit"].endsWith("refused") && !existsSync(join(mine, ".git", "hooks", "pre-commit")),
    "a protected folder refused": found[".vscode/settings.json"].endsWith("refused"),
  });
  expect(failed).toEqual([]);
});

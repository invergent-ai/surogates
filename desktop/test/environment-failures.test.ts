// What the environment does when the system cannot name the user. The calls are
// real unless a test switches userInfo to fail.

import { mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { appEnvironment, loginPath } from "../src/hosts/environment.js";

const failing = vi.hoisted(() => ({ userInfo: false }));

vi.mock("node:os", async (original) => {
  const os = await original<typeof import("node:os")>();
  return {
    ...os,
    // What it throws when the user's id has no passwd entry.
    userInfo: ((...args: Parameters<typeof os.userInfo>) => {
      if (failing.userInfo) throw new Error("ENOENT: no such user, uv_os_get_passwd");
      return os.userInfo(...args);
    }) as typeof os.userInfo,
  };
});

let base: string;
const saved = { USER: process.env.USER, LOGNAME: process.env.LOGNAME };

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "environment-failures-")));
});

afterEach(() => {
  failing.userInfo = false;
  for (const [name, value] of Object.entries(saved)) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
  rmSync(base, { recursive: true, force: true });
});

function shell(body: string): string {
  const path = join(base, "shell");
  writeFileSync(path, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  return path;
}

it("goes on when the system cannot name the user", async () => {
  failing.userInfo = true;
  delete process.env.USER;
  delete process.env.LOGNAME;
  expect(await loginPath(shell("printf '__P__/x__P__'"), base)).toBe("/x");
  const env = await appEnvironment({ shell: shell("printf '__P__/x__P__'"), home: base });
  expect(env).toMatchObject({ PATH: "/x", USER: "user", LOGNAME: "user" });
});

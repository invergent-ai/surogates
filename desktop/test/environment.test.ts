import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir, userInfo } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { absolutePath, appEnvironment, commandEnvironment, loginPath, makeCaches } from "../src/hosts/environment.js";

let base: string;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "environment-")));
});

afterEach(() => {
  rmSync(base, { recursive: true, force: true });
});

function shell(body: string): string {
  const path = join(base, "shell");
  writeFileSync(path, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  return path;
}

// Whether a process has gone, allowing a moment for the kill to land.
async function gone(pid: number): Promise<boolean> {
  for (let attempt = 0; attempt < 40; attempt++) {
    try {
      process.kill(pid, 0);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ESRCH") return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  return false;
}

// The pid a fake shell's background job wrote next to the shell.
const jobPid = () => Number(readFileSync(join(base, "shell.pid"), "utf8"));

describe("absolutePath", () => {
  it("keeps absolute entries once each, and drops empty and relative ones", () => {
    expect(absolutePath("/a:/b::.:node_modules/.bin:/a:/c")).toBe("/a:/b:/c");
    expect(absolutePath("")).toBe("");
  });
});

describe("loginPath", () => {
  it("reads the PATH the login shell prints", async () => {
    expect(await loginPath(shell(`printf 'noise __P__/x/bin:/y__P__ more'`), base)).toBe("/x/bin:/y");
  });

  it("answers null when the shell prints nothing it can use, or hangs", async () => {
    expect(await loginPath(shell("echo nothing"), base)).toBeNull();
    expect(await loginPath(shell("exec sleep 30"), base, 200)).toBeNull();
    expect(await loginPath(join(base, "missing-shell"), base)).toBeNull();
  });

  it("does not wait for what the shell leaves running", async () => {
    const started = Date.now();
    expect(await loginPath(shell("sleep 37.25 & printf '__P__/x__P__'"), base, 5_000)).toBe("/x");
    expect(await loginPath(shell("sleep 37.25 & exec sleep 37.25"), base, 200)).toBeNull();
    expect(Date.now() - started).toBeLessThan(2_000);
  });

  it("runs from the home folder, never from a project folder", async () => {
    expect(await loginPath(shell(`printf '__P__%s__P__' "$(pwd)"`), base)).toBe(base);
  });

  it("kills what the shell left running, once the PATH is out", async () => {
    expect(await loginPath(shell(`sleep 37.25 & echo $! > "$0.pid"; printf '__P__/x__P__'`), base)).toBe("/x");
    expect(await gone(jobPid())).toBe(true);
  });

  it("kills what the shell left running, at the timeout", async () => {
    expect(await loginPath(shell(`sleep 37.25 & echo $! > "$0.pid"; exec sleep 37.25`), base, 200)).toBeNull();
    expect(await gone(jobPid())).toBe(true);
  });

  it("kills the group once, however many ways there are to finish", async () => {
    const kill = vi.spyOn(process, "kill");
    try {
      expect(await loginPath(shell(`printf '__P__/x__P__'`), base)).toBe("/x");
      await new Promise((resolve) => setTimeout(resolve, 400)); // the shell's exit and close come after the answer
      expect(kill.mock.calls.filter(([pid]) => pid < 0)).toHaveLength(1);
    } finally {
      kill.mockRestore();
    }
  });

  it("stops waiting soon after the shell exits, though a job it left holds the output open", async () => {
    const started = Date.now();
    expect(await loginPath(shell("sleep 37.25 & exit 0"), base, 5_000)).toBeNull();
    expect(Date.now() - started).toBeLessThan(2_000);
  });

  it("finds the PATH after more output than it keeps", async () => {
    const flood = `head -c 3000000 /dev/zero | tr '\\0' x; printf '__P__/x__P__'`;
    expect(await loginPath(shell(flood), base)).toBe("/x");
  });

  it("does not hand the shell this process's environment", async () => {
    process.env.SURO_TEST_SECRET = "s3cret";
    try {
      expect(await loginPath(shell(`printf '__P__%s__P__' "\${SURO_TEST_SECRET:-none}"`), base)).toBe("none");
    } finally {
      delete process.env.SURO_TEST_SECRET;
    }
  });
});

describe("appEnvironment", () => {
  it("is the whitelisted names, with an absolute-only PATH", async () => {
    const env = await appEnvironment({ shell: shell(`printf '__P__/opt/x:relative:/usr/bin__P__'`), home: base });
    expect(Object.keys(env).sort()).toEqual(["HOME", "LANG", "LOGNAME", "PATH", "TERM", "USER"]);
    expect(env).toMatchObject({ HOME: base, PATH: "/opt/x:/usr/bin", TERM: "dumb", USER: userInfo().username });
  });

  it("falls back to this process's PATH when the login shell gives none, or none it can use", async () => {
    const own = absolutePath(process.env.PATH ?? "") || "/usr/bin:/bin";
    expect((await appEnvironment({ shell: shell("true"), home: base })).PATH).toBe(own);
    expect((await appEnvironment({ shell: shell("printf '__P____P__'"), home: base })).PATH).toBe(own);
    expect((await appEnvironment({ shell: shell("printf '__P__rel:.:bin__P__'"), home: base })).PATH).toBe(own);
  });
});

describe("commandEnvironment", () => {
  it("keeps only the app's names, re-checks PATH, and adds the session's temp folder and caches", () => {
    const tmp = join(base, "tmp");
    const env = commandEnvironment(
      { HOME: "/h", LANG: "C.UTF-8", PATH: "/a:.::/b", SECRET: "s", BASH_ENV: "/x", LD_PRELOAD: "/y.so", NODE_OPTIONS: "-r x" },
      tmp,
    );
    expect(env).toEqual({
      HOME: "/h", LANG: "C.UTF-8", PATH: "/a:/b", TMPDIR: tmp,
      XDG_CACHE_HOME: join(tmp, "cache"), npm_config_cache: join(tmp, "npm"),
      PIP_CACHE_DIR: join(tmp, "pip"), UV_CACHE_DIR: join(tmp, "uv"),
    });
  });

  it("falls back to the system PATH when the app's has no absolute entry", () => {
    const tmp = join(base, "tmp");
    expect(commandEnvironment({ PATH: "rel:." }, tmp).PATH).toBe("/usr/bin:/bin");
    expect(commandEnvironment({}, tmp).PATH).toBe("/usr/bin:/bin");
  });

  it("makes the cache folders", () => {
    const tmp = join(base, "tmp");
    makeCaches(tmp);
    for (const name of ["cache", "npm", "pip", "uv"]) expect(existsSync(join(tmp, name))).toBe(true);
  });
});

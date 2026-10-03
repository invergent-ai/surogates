import { existsSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir, userInfo } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

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
});

describe("appEnvironment", () => {
  it("is the whitelisted names, with an absolute-only PATH", async () => {
    const env = await appEnvironment({ shell: shell(`printf '__P__/opt/x:relative:/usr/bin__P__'`), home: base });
    expect(Object.keys(env).sort()).toEqual(["HOME", "LANG", "LOGNAME", "PATH", "TERM", "USER"]);
    expect(env).toMatchObject({ HOME: base, PATH: "/opt/x:/usr/bin", TERM: "dumb", USER: userInfo().username });
  });

  it("falls back to this process's PATH when the login shell gives none", async () => {
    const env = await appEnvironment({ shell: shell("true"), home: base });
    expect(env.PATH).toBe(absolutePath(process.env.PATH ?? "") || "/usr/bin:/bin");
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

  it("makes the cache folders", () => {
    const tmp = join(base, "tmp");
    makeCaches(tmp);
    for (const name of ["cache", "npm", "pip", "uv"]) expect(existsSync(join(tmp, name))).toBe(true);
  });
});

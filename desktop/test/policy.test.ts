import { mkdirSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { isReserved, sandboxPolicy } from "../src/hosts/policy.js";

let base = "";
afterEach(() => rmSync(base, { recursive: true, force: true }));

describe("the sandbox policy", () => {
  it("makes srt's own /tmp/claude read-only, because every srt sandbox shares it", () => {
    const { filesystem } = sandboxPolicy({ folder: "/f", tmp: "/t", home: "/h", appDirs: [] });
    expect(filesystem.denyWrite).toEqual(["/tmp/claude", "/private/tmp/claude"]);
    expect(filesystem.allowWrite).toEqual(["/f", "/t"]);
  });

  it("re-admits the toolchains that exist, and none whose path srt would read as a glob", () => {
    base = realpathSync(mkdtempSync(join(tmpdir(), "policy-")));
    const plain = join(base, "plain");
    const odd = join(base, "odd[1]");
    for (const home of [plain, odd]) mkdirSync(join(home, ".nvm"), { recursive: true });
    const read = (home: string) => sandboxPolicy({ folder: "/f", tmp: "/t", home, appDirs: [] }).filesystem.allowRead;
    expect(read(plain)).toContain(join(plain, ".nvm"));
    expect(read(odd)).not.toContain(join(odd, ".nvm"));
  });
});

describe("the folders no sandbox may be given", () => {
  it.each(["/proc", "/proc/self", "/sys/kernel", "/dev", "/dev/shm", "/run", "/run/user/1000", "/run/media-x"])(
    "refuses %s",
    (path) => expect(isReserved(path)).toBe(true),
  );

  it.each(["/run/media/flavius/USB", "/run/media/flavius/USB/work", "/home/flavius/work", "/tmp/work", "/tmp/claudex", "/runner"])(
    "accepts %s, so a removable drive can be bound",
    (path) => expect(isReserved(path)).toBe(false),
  );

  it.each(["/tmp/claude", "/tmp/claude/work", "/private/tmp/claude", "/private/tmp/claude/work"])(
    "refuses %s, which srt makes read-only",
    (path) => expect(isReserved(path)).toBe(true),
  );
});

import { mkdirSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { hideSrtTmp, isReserved, PACKAGE_HOSTS, sandboxPolicy } from "../src/hosts/policy.js";

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

describe("the package hosts", () => {
  it("are the cloud's list without its coding-agent endpoints", () => {
    expect(PACKAGE_HOSTS).toEqual([
      "github.com", "*.github.com", "*.githubusercontent.com", "pypi.org", "*.pypi.org", "files.pythonhosted.org",
      "npmjs.org", "*.npmjs.org", "registry.npmjs.org",
    ]);
  });

  it("are what the sandbox lets commands reach, with srt's tools by their absolute paths", () => {
    const policy = sandboxPolicy({
      folder: "/f", tmp: "/t", home: "/h", appDirs: [], bwrapPath: "/b/bwrap", socatPath: "/s/socat", rgPath: "/r/rg",
    });
    expect(policy.network).toEqual({ allowedDomains: PACKAGE_HOSTS, deniedDomains: [] });
    expect(policy).toMatchObject({ bwrapPath: "/b/bwrap", socatPath: "/s/socat" });
    // srt's scan for nested protected names must not read the folder's ignore files.
    expect(policy.ripgrep).toEqual({ command: "/r/rg", args: ["--no-ignore"] });
  });
});

describe("hideSrtTmp", () => {
  const line = "/usr/bin/bwrap --new-session --ro-bind / / --bind /tmp/claude /tmp/claude --dev /dev --unshare-pid --unshare-user -- bash -c 'x --dev /dev --unshare-pid y'";

  it("puts an empty tmpfs over /tmp/claude after srt's mounts, before the command", () => {
    expect(hideSrtTmp(line)).toBe(
      "/usr/bin/bwrap --new-session --ro-bind / / --bind /tmp/claude /tmp/claude --tmpfs /tmp/claude --dev /dev --unshare-pid --unshare-user -- bash -c 'x --dev /dev --unshare-pid y'",
    );
  });

  it("finds the anchor among the line's words, never inside a path srt quoted", () => {
    const steered = "/usr/bin/bwrap --ro-bind / / --ro-bind /dev/null '/f/q --dev /dev --unshare-pid /.bashrc' --dev /dev --unshare-pid -- bash";
    expect(hideSrtTmp(steered)).toBe(
      "/usr/bin/bwrap --ro-bind / / --ro-bind /dev/null '/f/q --dev /dev --unshare-pid /.bashrc' --tmpfs /tmp/claude --dev /dev --unshare-pid -- bash",
    );
    expect(() => hideSrtTmp("/usr/bin/bwrap --ro-bind /dev/null '/f/q --dev /dev --unshare-pid /x' -- bash")).toThrow(/cannot hide/);
  });

  it("reads srt's quote inside a quoted word, as in a folder named with an apostrophe", () => {
    // srt writes an apostrophe in a quoted word as '"'"': the anchor after it is still inside the word.
    const named = `/usr/bin/bwrap --ro-bind /dev/null '/f/it'"'"'s x --dev /dev --unshare-pid /.vscode' --dev /dev --unshare-pid -- bash`;
    expect(hideSrtTmp(named)).toBe(
      `/usr/bin/bwrap --ro-bind /dev/null '/f/it'"'"'s x --dev /dev --unshare-pid /.vscode' --tmpfs /tmp/claude --dev /dev --unshare-pid -- bash`,
    );
  });

  it("refuses a line it does not recognise, so no sandbox starts with /tmp/claude showing", () => {
    expect(() => hideSrtTmp("/usr/bin/bwrap --ro-bind / / -- bash")).toThrow(/cannot hide \/tmp\/claude/);
  });
});

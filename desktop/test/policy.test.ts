import { describe, expect, it } from "vitest";

import { hideSrtTmp, isReserved, sandboxPolicy } from "../src/hosts/policy.js";

describe("the file helper's sandbox policy", () => {
  it("reads the system, the app, the folder and its working folder, writes the last two, and reaches no host", () => {
    expect(sandboxPolicy({ folder: "/f", tmp: "/t", appDirs: ["/app"], bwrapPath: "/b/bwrap", socatPath: "/s/socat" })).toEqual({
      bwrapPath: "/b/bwrap",
      socatPath: "/s/socat",
      network: { allowedDomains: [], deniedDomains: [] },
      filesystem: {
        denyRead: ["/"],
        allowRead: ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/proc", "/sys", "/dev", "/run/systemd/resolve", "/app", "/f", "/t"],
        allowWrite: ["/f", "/t"],
        // srt's own /tmp/claude, read-only, because every srt sandbox shares it.
        denyWrite: ["/tmp/claude", "/private/tmp/claude"],
      },
    });
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

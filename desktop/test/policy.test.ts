import { chmodSync, mkdirSync, mkdtempSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { fileToolsMissing, hideSrtTmp, isReserved, pathOutside, sandboxPolicy } from "../src/hosts/policy.js";

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

describe("the file helper's tools on this computer", () => {
  it("names each the helper's sandbox lacks as the install script installs it: the version's bwrap, or bwrap, socat and rg on the PATH", () => {
    const bin = mkdtempSync(join(tmpdir(), "file-tools-"));
    const tool = (name: string) => {
      writeFileSync(join(bin, name), "#!/bin/sh\n");
      chmodSync(join(bin, name), 0o755);
    };
    try {
      expect(fileToolsMissing(undefined, bin)).toEqual(["bubblewrap", "socat", "ripgrep"]);
      tool("bwrap");
      tool("socat");
      tool("rg");
      expect(fileToolsMissing(undefined, bin)).toEqual([]);
      // The installed app's own copy, beside its version: one on the PATH does not stand in for it.
      expect(fileToolsMissing(join(bin, "bin", "bwrap"), bin)).toEqual(["bubblewrap"]);
      expect(fileToolsMissing(join(bin, "bwrap"), bin)).toEqual([]);
      // A file that cannot be run is not the tool.
      chmodSync(join(bin, "socat"), 0o644);
      expect(fileToolsMissing(undefined, bin)).toEqual(["socat"]);
    } finally {
      rmSync(bin, { recursive: true, force: true });
    }
  });
});

describe("the PATH the file helper's tools are looked for on", () => {
  let base: string;
  let folder: string;
  let tools: string;
  const tool = (dir: string, name: string) => {
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, name), "#!/bin/sh\n", { mode: 0o755 });
  };

  beforeEach(() => {
    base = realpathSync(mkdtempSync(join(tmpdir(), "tools-path-")));
    folder = join(base, "folder");
    tools = join(base, "tools");
    mkdirSync(folder);
    tool(tools, "socat");
    tool(tools, "rg");
  });

  afterEach(() => rmSync(base, { recursive: true, force: true }));

  it("keeps each absolute entry as the folder it leads to now, and none that is relative; with none left, the system's own", () => {
    symlinkSync(tools, join(base, "link"));
    expect(pathOutside(`bin:${join(base, "link")}::${folder}:.:${folder}/../tools`, [])).toBe(`${tools}:${folder}:${tools}`);
    expect(pathOutside("bin::.", [])).toBe("/usr/bin:/bin");
    expect(pathOutside(undefined, [])).toBe("/usr/bin:/bin");
  });

  it("drops an entry that is a folder a command may write or lies in one, however it is spelled, and one in a loop of links", () => {
    mkdirSync(join(folder, "bin"));
    symlinkSync(join(folder, "bin"), join(base, "alias"));
    symlinkSync("loop", join(base, "loop"));
    const entries = [folder, join(folder, "bin"), join(base, "alias"), `${tools}/../folder/bin`, join(base, "loop", "bin"), tools];
    expect(pathOutside(entries.join(":"), [statSync(folder)])).toBe(tools);
    // Held by nobody, the folder's entries are entries like any other.
    expect(pathOutside(`${join(folder, "bin")}:${tools}`, [])).toBe(`${join(folder, "bin")}:${tools}`);
  });

  it.each([
    // Spelled from the root, where a look that took the PATH as it is would read it.
    ["a relative entry of the PATH", () => {
      tool(join(base, "relative"), "bwrap");
      return join(base, "relative").slice(1);
    }],
    ["an entry of the PATH inside the folder", () => {
      tool(join(folder, "bin"), "bwrap");
      return join(folder, "bin");
    }],
  ])("finds no bubblewrap whose only copy is in %s: the app looks where the folder's file host does", (_name, entry) => {
    const path = `${entry()}:${tools}`;
    mkdirSync(join(base, "working"));
    // The copy is there, for a look that takes the PATH as it is.
    expect(fileToolsMissing(undefined, path)).toEqual([]);
    // The file host holds its folder and its working folder; the app, every folder its chats are bound to.
    for (const held of [[statSync(folder), statSync(join(base, "working"))], [statSync(folder)]]) {
      expect(fileToolsMissing(undefined, pathOutside(path, held))).toEqual(["bubblewrap"]);
    }
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

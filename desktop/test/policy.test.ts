import { mkdirSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { isIP } from "node:net";
import { networkInterfaces, tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { destination, hideSrtTmp, isReserved, PACKAGE_HOSTS, reach, sandboxPolicy } from "../src/hosts/policy.js";

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

  it("scans for nested protected names as deep as srt allows", () => {
    expect(sandboxPolicy({ folder: "/f", tmp: "/t", home: "/h", appDirs: [] }).mandatoryDenySearchDepth).toBe(10);
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

describe("a destination", () => {
  it.each([
    ["Example.COM.", 443, "example.com"],
    ["127.1", 8080, "127.0.0.1"],
    ["::1", 3000, "[::1]"],
    ["[::1]", 3000, "[::1]"],
    ["::ffff:127.0.0.1", 80, "127.0.0.1"],
    ["[::ffff:c000:201]", 9, "192.0.2.1"],
    ["files_1.example-cdn.net", 80, "files_1.example-cdn.net"],
  ])("is spelled as srt compares it: %s", (host, port, spelled) => {
    expect(destination(host, port)).toEqual({ host: spelled, port });
  });

  it.each([
    ["*.example.com", 443], ["example.com", 0], ["example.com", 65_536], ["example.com", 1.5], ["example.com", undefined],
    ["", 443], ["a b", 443], ["a..b", 443],
  ])("is refused for %s, port %s, so no grant can let more through", (host, port) => {
    expect(destination(host, port)).toBeNull();
  });
});

describe("where a destination leads", () => {
  // This computer's own addresses, as its interfaces would give them.
  const local = () => ["127.0.0.1", "::1", "192.168.100.139", "fe80::2d6:c59:8d66:3938"];
  const names: Record<string, string[]> = {
    "printer.lan": ["192.168.1.20"],
    "example.com": ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
    "sneaky.example": ["93.184.215.14", "127.0.0.1"],
    "mine.example": ["192.168.100.139"],
    "odd.example": ["not an address"],
  };
  const resolve = (name: string) => (name in names ? Promise.resolve(names[name] ?? []) : Promise.reject(new Error("ENOTFOUND")));
  const where = (host: string) => reach(host, { local, resolve });

  it.each([
    "127.0.0.1", "127.8.9.10", "[::1]", "0.0.0.0", "[::]", "[::ffff:7f00:1]", "192.168.100.139", "[fe80::2d6:c59:8d66:3938]",
    "localhost", "dev.localhost", "sneaky.example", "mine.example",
    "169.254.169.254", "100.100.100.200", "168.63.129.16", "[fd00:ec2::254]",
  ])("is this computer for %s", async (host) => {
    expect(await where(host)).toBe("own");
  });

  it.each(["10.1.2.3", "172.16.0.1", "192.168.1.1", "100.101.2.3", "169.254.1.1", "[fd00::1]", "[fe80::1]", "printer.lan"])(
    "is a private network for %s",
    async (host) => {
      expect(await where(host)).toBe("private");
    },
  );

  it.each(["192.0.2.1", "[2001:db8::1]", "example.com"])("is elsewhere for %s", async (host) => {
    expect(await where(host)).toBe("public");
  });

  it("is unknown for a name that cannot be looked up, gives no address, or takes too long", async () => {
    expect(await where("nowhere.invalid")).toBeNull();
    // A lookup that fails: the resolver rejects.
    expect(await where("nowhere.example")).toBeNull();
    // RFC 6761: decided without a lookup, so a resolver that answers for it changes nothing.
    expect(await reach("surogate-test.invalid", { local, resolve: () => Promise.resolve(["93.184.215.14"]) })).toBeNull();
    expect(await where("odd.example")).toBeNull();
    const started = performance.now();
    expect(await reach("slow.example", { local, resolve: () => new Promise(() => {}), timeoutMs: 50 })).toBeNull();
    expect(performance.now() - started).toBeLessThan(1_000);
  });

  // An address of this computer's own interfaces, other than loopback.
  const lan = Object.values(networkInterfaces()).flat().find((entry) => entry && !entry.internal)?.address;
  it.skipIf(!lan)("reads this computer's own addresses from its interfaces by default", async () => {
    const address = lan ?? "";
    expect(await reach(isIP(address) === 6 ? `[${address}]` : address)).toBe("own");
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

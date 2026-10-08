import { isIP } from "node:net";
import { networkInterfaces } from "node:os";

import { describe, expect, it } from "vitest";

import { destination, judge, LOOKUP_MS, PACKAGE_HOSTS, packageHost, reach } from "../src/vm/egress.js";

// This computer's own addresses, as its interfaces would give them.
const local = () => ["127.0.0.1", "::1", "192.168.100.139", "fe80::2d6:c59:8d66:3938"];
// The networks this computer is on: none here, so no test reads the machine's own.
const subnets = () => [];
const names: Record<string, string[]> = {
  "printer.lan": ["192.168.1.20"],
  "example.com": ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
  "sneaky.example": ["93.184.215.14", "127.0.0.1"],
  "mine.example": ["192.168.100.139"],
  // This computer's own, as a resolver can give them: IPv4-mapped.
  "mapped-lo.example": ["::ffff:127.0.0.1"],
  "mapped-lan.example": ["::ffff:192.168.100.139"],
  // A public address and a private one.
  "mixed.example": ["93.184.215.14", "10.0.0.1"],
  "odd.example": ["not an address"],
  "pypi.org": ["151.101.0.223"],
  "evil.pypi.org": ["127.0.0.1"],
};
const resolve = (name: string) => (name in names ? Promise.resolve(names[name] ?? []) : Promise.reject(new Error("ENOTFOUND")));

describe("the package hosts", () => {
  it("are the cloud's list without its coding-agent endpoints", () => {
    expect(PACKAGE_HOSTS).toEqual([
      "github.com", "*.github.com", "*.githubusercontent.com", "pypi.org", "*.pypi.org", "files.pythonhosted.org",
      "npmjs.org", "*.npmjs.org", "registry.npmjs.org",
    ]);
  });

  it.each(["github.com", "api.github.com", "objects.githubusercontent.com", "pypi.org", "test.pypi.org", "files.pythonhosted.org", "registry.npmjs.org"])(
    "take %s, as srt matched them",
    (host) => expect(packageHost(host)).toBe(true),
  );

  it.each(["githubusercontent.com", "evilgithub.com", "github.com.evil.example", "pythonhosted.org", "example.com", "[::1]", "127.0.0.1"])(
    "leave out %s",
    (host) => expect(packageHost(host)).toBe(false),
  );
});

describe("a destination", () => {
  it.each([
    ["Example.COM.", 443, "example.com"],
    ["127.1", 8080, "127.0.0.1"],
    // Decimal, octal and hex, as WHATWG URL reads them.
    ["2130706433", 80, "127.0.0.1"],
    ["0177.0.0.1", 80, "127.0.0.1"],
    ["0x7f.1", 80, "127.0.0.1"],
    ["::1", 3000, "[::1]"],
    ["[::1]", 3000, "[::1]"],
    ["::ffff:127.0.0.1", 80, "127.0.0.1"],
    ["[::ffff:c000:201]", 9, "192.0.2.1"],
    ["files_1.example-cdn.net", 80, "files_1.example-cdn.net"],
  ])("is spelled as a grant names it: %s", (host, port, spelled) => {
    expect(destination(host, port)).toEqual({ host: spelled, port });
  });

  it.each([
    ["*.example.com", 443], ["example.com", 0], ["example.com", 65_536], ["example.com", 1.5], ["example.com", undefined],
    ["", 443], ["a b", 443], ["a..b", 443], ["::ffff:1.2.3.4%x.pypi.org", 443],
  ])("is refused for %s, port %s, so no grant can let more through", (host, port) => {
    expect(destination(host, port)).toBeNull();
  });
});

describe("where a destination leads", () => {
  const where = async (host: string) => (await reach(host, { local, subnets, resolve }))?.reach ?? null;

  it.each([
    "127.0.0.1", "127.8.9.10", "[::1]", "0.0.0.0", "[::]", "[::ffff:7f00:1]", "192.168.100.139", "[fe80::2d6:c59:8d66:3938]",
    "localhost", "dev.localhost", "sneaky.example", "mine.example", "mapped-lo.example", "mapped-lan.example",
    "169.254.169.254", "100.100.100.200", "168.63.129.16", "[fd00:ec2::254]",
    // IPv4-compatible and IPv4-translated loopback, which no stack is trusted to refuse.
    "[::127.0.0.1]", "[::ffff:0:7f00:1]", "[::ffff:0:c000:201]",
  ])("is this computer for %s", async (host) => {
    expect(await where(host)).toBe("own");
  });

  it.each(["10.1.2.3", "172.16.0.1", "192.168.1.1", "100.101.2.3", "169.254.1.1", "[fd00::1]", "[fe80::1]", "printer.lan", "mixed.example"])(
    "is a private network for %s",
    async (host) => {
      expect(await where(host)).toBe("private");
    },
  );

  // NAT64 is how an IPv6-only network reaches IPv4 sites.
  it.each(["192.0.2.1", "[2001:db8::1]", "example.com", "[64:ff9b::c000:201]"])("is elsewhere for %s", async (host) => {
    expect(await where(host)).toBe("public");
  });

  it("is a private network for an address on a network this computer is on, a public IPv4 range or a global IPv6 prefix", async () => {
    // As its interfaces give them: each address with its prefix.
    const on = async (host: string) => (await reach(host, {
      local: () => ["198.51.100.5", "2001:db8:1::5"], subnets: () => ["198.51.100.5/24", "2001:db8:1::5/64"], resolve,
    }))?.reach;
    expect(await on("198.51.100.7")).toBe("private");
    expect(await on("[2001:db8:1::1234]")).toBe("private");
    expect(await on("198.51.101.7")).toBe("public");
    expect(await on("[2001:db8:2::1]")).toBe("public");
  });

  it("gives the addresses it judged, each without brackets, from one lookup", async () => {
    let lookups = 0;
    const counted = (name: string) => {
      lookups += 1;
      return resolve(name);
    };
    expect(await reach("example.com", { local, subnets, resolve: counted })).toEqual({
      reach: "public", addresses: ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
    });
    expect(await reach("[2001:db8::1]", { local, subnets, resolve: counted })).toEqual({ reach: "public", addresses: ["2001:db8::1"] });
    expect(lookups).toBe(1);
  });

  it("is unknown for a name that cannot be looked up, gives no address, or takes too long", async () => {
    expect(await where("nowhere.invalid")).toBeNull();
    // A lookup that fails: the resolver rejects.
    expect(await where("nowhere.example")).toBeNull();
    // RFC 6761: decided without a lookup, so a resolver that answers for it changes nothing.
    expect(await reach("surogate-test.invalid", { local, subnets, resolve: () => Promise.resolve(["93.184.215.14"]) })).toBeNull();
    expect(await where("odd.example")).toBeNull();
    const started = performance.now();
    expect(await reach("slow.example", { local, subnets, resolve: () => new Promise(() => {}), timeoutMs: 50 })).toBeNull();
    expect(performance.now() - started).toBeLessThan(1_000);
    // Unless told otherwise, as the host proxy is not.
    expect(LOOKUP_MS).toBe(2_000);
  });

  // An address of this computer's own interfaces, other than loopback.
  const lan = Object.values(networkInterfaces()).flat().find((entry) => entry && !entry.internal)?.address;
  it.skipIf(!lan)("reads this computer's own addresses from its interfaces by default", async () => {
    const address = lan ?? "";
    expect((await reach(isIP(address) === 6 ? `[${address}]` : address))?.reach).toBe("own");
  });
});

describe("a connection, judged", () => {
  const judged = (host: string, port: number) => judge(host, port, { local, subnets, resolve });

  it("lets a package host through without asking, to the addresses it judged", async () => {
    expect(await judged("pypi.org", 443)).toEqual({ key: "pypi.org:443", dial: ["151.101.0.223"], ask: null });
  });

  it("refuses this computer's own before the package hosts: a package host's name that leads here is refused", async () => {
    expect(await judged("evil.pypi.org", 443)).toEqual({ refused: "own", key: "evil.pypi.org:443" });
    expect(await judged("127.1", 9)).toEqual({ refused: "own", key: "127.0.0.1:9" });
    expect(await judged("2130706433", 9)).toEqual({ refused: "own", key: "127.0.0.1:9" });
    expect(await judged("0x7f.1", 9)).toEqual({ refused: "own", key: "127.0.0.1:9" });
    expect(await judged("mapped-lo.example", 9)).toEqual({ refused: "own", key: "mapped-lo.example:9" });
    expect(await judged("Sneaky.Example.", 443)).toEqual({ refused: "own", key: "sneaky.example:443" });
  });

  it("asks about the rest, saying when it is a private network, and dials what it judged once allowed", async () => {
    expect(await judged("example.com", 443)).toEqual({
      key: "example.com:443", dial: ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"], ask: { host: "example.com", port: 443, privateNetwork: false },
    });
    expect(await judged("printer.lan", 631)).toEqual({ key: "printer.lan:631", dial: ["192.168.1.20"], ask: { host: "printer.lan", port: 631, privateNetwork: true } });
    // A public address and a private one: asked as a private network, both kept for the dial.
    expect(await judged("mixed.example", 443)).toEqual({
      key: "mixed.example:443", dial: ["93.184.215.14", "10.0.0.1"], ask: { host: "mixed.example", port: 443, privateNetwork: true },
    });
    expect(await judged("fd00::1", 22)).toEqual({ key: "[fd00::1]:22", dial: ["fd00::1"], ask: { host: "[fd00::1]", port: 22, privateNetwork: true } });
  });

  it("refuses a name it cannot look up, and what is no destination, without asking", async () => {
    expect(await judged("nowhere.example", 443)).toEqual({ refused: "unknown", key: "nowhere.example:443" });
    expect(await judged("*.example.com", 443)).toEqual({ refused: "invalid" });
    expect(await judged("example.com", 0)).toEqual({ refused: "invalid" });
    expect(await judge("example.com", 443, { local: () => { throw new Error("no interfaces"); }, subnets, resolve })).toEqual({ refused: "unknown", key: "example.com:443" });
  });
});

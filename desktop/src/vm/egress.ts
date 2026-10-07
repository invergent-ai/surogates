// What a guest's command may reach (spec, Section 11, Network), as the host proxy
// judges each connection when it is made: the destination as the command spelled
// it, where it leads, and the addresses the host then dials, the ones it judged.

import { lookup } from "node:dns/promises";
import { BlockList, connect as connectTcp, isIP, isIPv6, type Socket } from "node:net";
import { networkInterfaces } from "node:os";

import type { Destination, NetworkAsk } from "../hosts/messages.js";

// The package hosts commands may reach without asking: the cloud's list
// (surogates/tools/workspace_io/local.py) without its coding-agent endpoints.
export const PACKAGE_HOSTS = [
  "github.com", "*.github.com", "*.githubusercontent.com", "pypi.org", "*.pypi.org", "files.pythonhosted.org",
  "npmjs.org", "*.npmjs.org", "registry.npmjs.org",
];

// A destination as a grant names it: lower case, IPv4 shorthand spelled out, IPv6 in
// brackets, no trailing dot. Null for one no connection may go to.
export function destination(host: string, port: number | undefined): Destination | null {
  if (port === undefined || !Number.isInteger(port) || port < 1 || port > 65_535) return null;
  const bare = host.startsWith("[") && host.endsWith("]") ? host.slice(1, -1) : host;
  let name: string;
  try {
    name = new URL(`http://${isIP(bare) === 6 ? `[${bare}]` : bare}/`).hostname.replace(/\.$/, "");
  } catch {
    return null;
  }
  // An IPv4-mapped address is the IPv4 one, which is what is dialed.
  const mapped = /^\[::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})\]$/.exec(name);
  if (mapped) {
    const [hi, lo] = [Number.parseInt(mapped[1] ?? "", 16), Number.parseInt(mapped[2] ?? "", 16)];
    name = `${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`;
  }
  // Never a pattern: a grant with a * would let a whole domain through.
  return /^([a-z0-9_-]+(\.[a-z0-9_-]+)*|\[[0-9a-f:.]+\])$/.test(name) ? { host: name, port } : null;
}

// Whether *host*, as destination() spells it, is a package host: "*.x" is any name under
// x, never x itself, as srt matched its allowedDomains. No address ends in a name.
export function packageHost(host: string): boolean {
  return PACKAGE_HOSTS.some((pattern) => (pattern.startsWith("*.") ? host.endsWith(pattern.slice(1)) : host === pattern));
}

// Where a destination leads: this computer itself, a private network, or elsewhere.
export type Reach = "own" | "private" | "public";

// Where it leads, and the addresses that was judged on: the host dials these and no others.
export interface Reached {
  reach: Reach;
  addresses: string[];
}

// How long a name may take to look up before the connection is refused.
export const LOOKUP_MS = 2_000;

const subnets = (ranges: Array<[string, number]>) => {
  const list = new BlockList();
  for (const [net, prefix] of ranges) list.addSubnet(net, prefix, isIP(net) === 6 ? "ipv6" : "ipv4");
  return list;
};
// Loopback, and the unspecified address, which connects to this computer on common stacks;
// in IPv6 within IPv4-compatible addresses (::/96), and IPv4-translated ones (::ffff:0:0:0/96):
// nothing dials these, and a stack that takes them can reach loopback by them.
const LOCAL = subnets([["127.0.0.0", 8], ["0.0.0.0", 8], ["::", 96], ["::ffff:0:0:0", 96]]);
// Cloud instance-metadata endpoints, which answer for the machine itself: the common
// link-local one, and srt's CLOUD_METADATA_ADDRESSES (resolved-address-guard.js).
const METADATA = subnets([
  ["169.254.169.254", 32], ["100.100.100.200", 32], ["168.63.129.16", 32], ["192.0.0.192", 32], ["fd00:ec2::", 32],
  ["fd20:ce::254", 128], ["fd00:c1::a9fe:a9fe", 128], ["fd00:42::42", 128], ["fd00:a9fe:a9fe::1", 128], ["fd00:100::100:200", 128],
]);
// RFC 1918, shared address space (CGNAT, Tailscale), link-local and unique-local: asked about, with the prompt saying so.
const PRIVATE = subnets([
  ["10.0.0.0", 8], ["172.16.0.0", 12], ["192.168.0.0", 16], ["100.64.0.0", 10], ["169.254.0.0", 16], ["fc00::", 7],
  ["fe80::", 10],
]);

const family = (address: string) => (isIP(address) === 6 ? "ipv6" : "ipv4");
const addressesOf = async (name: string) => (await lookup(name, { all: true })).map((entry) => entry.address);
const interfaces = () => Object.values(networkInterfaces()).flatMap((entries) => entries ?? []);
const interfaceAddresses = () => interfaces().map((entry) => entry.address);
const interfaceSubnets = () => interfaces().flatMap((entry) => (entry.cidr ? [entry.cidr] : []));

export interface ReachOptions {
  resolve?: (name: string) => Promise<string[]>; // a name's addresses; dns.lookup by default
  local?: () => string[]; // this computer's own addresses; its interfaces' by default, read at each call
  // The networks this computer is on, as address/prefix: private, whatever their range. Its interfaces' by default, read at each call.
  subnets?: () => string[];
  timeoutMs?: number;
}

/**
 * Where a destination from destination() leads, from one lookup, with the addresses it
 * gave. Any of them on this computer makes it "own": a command must not reach the user's
 * own services. Any in a private range, or on a network this computer is on, makes it
 * "private". Null when a name cannot be looked up in time. BlockList matches IPv4-mapped IPv6 too.
 */
export async function reach(host: string, options: ReachOptions = {}): Promise<Reached | null> {
  const { resolve = addressesOf, local = interfaceAddresses, subnets: near = interfaceSubnets, timeoutMs = LOOKUP_MS } = options;
  const bare = host.startsWith("[") ? host.slice(1, -1) : host;
  let addresses: string[] = [bare];
  if (!isIP(bare)) {
    // RFC 6761: whatever a resolver says, these are this computer, and these are nowhere.
    if (bare === "localhost" || bare.endsWith(".localhost")) return { reach: "own", addresses: [] };
    if (bare === "invalid" || bare.endsWith(".invalid")) return null;
    let timer: NodeJS.Timeout | undefined;
    const late = new Promise<string[]>((settle) => {
      timer = setTimeout(() => settle([]), timeoutMs);
    });
    addresses = await Promise.race([resolve(bare).catch(() => []), late]);
    clearTimeout(timer);
    addresses = addresses.filter((address) => isIP(address));
    if (addresses.length === 0) return null;
  }
  const mine = new BlockList();
  for (const address of local()) if (isIP(address)) mine.addAddress(address, family(address));
  const own = (address: string) => [LOCAL, METADATA, mine].some((list) => list.check(address, family(address)));
  if (addresses.some(own)) return { reach: "own", addresses };
  // A network this computer is on is private, a home LAN's global IPv6 prefix or a campus's public range too.
  const lans = new BlockList();
  for (const subnet of near()) {
    const [net, prefix] = subnet.split("/");
    if (net && isIP(net) && prefix !== undefined) lans.addSubnet(net, Number(prefix), family(net));
  }
  const inside = (address: string) => [PRIVATE, lans].some((list) => list.check(address, family(address)));
  return { reach: addresses.some(inside) ? "private" : "public", addresses };
}

// What the host proxy does with a connection, by its destination's *key* (host:port, as
// destination() spells them): dial these addresses, asking the chat's user first unless
// *ask* is null; or refuse it, as no destination at all, as this computer's own, or as a
// name it could not look up.
export type Verdict =
  | { refused: "invalid" }
  | { refused: "own" | "unknown"; key: string }
  | { key: string; dial: string[]; ask: NetworkAsk | null };

/**
 * A connection to *host*:*port*, judged in Section 11's order: a destination, then where
 * it leads, for every one, package hosts and grants included; then the package hosts
 * pass, and the rest ask (the grants are the asker's). Never rejects: a lookup or an
 * interface read that throws refuses, as a name that cannot be looked up does.
 */
export async function judge(host: string, port: number, options: ReachOptions = {}): Promise<Verdict> {
  const found = destination(host, port);
  if (!found) return { refused: "invalid" };
  const key = `${found.host}:${found.port}`;
  const where = await reach(found.host, options).catch(() => null);
  if (!where) return { refused: "unknown", key };
  if (where.reach === "own") return { refused: "own", key };
  return { key, dial: where.addresses, ask: packageHost(found.host) ? null : { ...found, privateNetwork: where.reach === "private" } };
}

// How long an attempt has to connect before the next judged address is tried beside it, the
// earlier one still running (RFC 8305), so an address that never answers holds no other back.
// Node's autoSelectFamily waits as long, then drops the earlier one.
export const STAGGER_MS = 250;

// The judged addresses, IPv6 and IPv4 by turns, always IPv6 first, whatever order the lookup
// gave them. Node's autoSelectFamily leads with the family of the lookup's first address.
export function inTurn(addresses: readonly string[]): string[] {
  const six = addresses.filter((address) => isIPv6(address));
  const four = addresses.filter((address) => !isIPv6(address));
  return Array.from({ length: Math.max(six.length, four.length) }, (_, at) => [six[at], four[at]]).flat().filter((address) => address !== undefined);
}

export type Dial = (address: string, port: number) => Socket;
const tcp: Dial = (address, port) => connectTcp({ host: address, port, allowHalfOpen: true });

/**
 * A connection to *port* at the judged *addresses* and no others, in turn (inTurn): the next at
 * once when one fails, or beside it once it has had STAGGER_MS; the first to connect is the one,
 * and the rest go. Rejects with the last failure's code (an Error whose message is the code) when
 * none connects, and once *signal* aborts, with every attempt ended.
 */
export function dialFirst(addresses: readonly string[], port: number, signal: AbortSignal, dial: Dial = tcp): Promise<Socket> {
  const order = inTurn(addresses);
  return new Promise((resolve, reject) => {
    const trying = new Set<Socket>();
    let next = 0;
    let failed = "EHOSTUNREACH";
    let settled = false;
    let stagger: NodeJS.Timeout | undefined;
    const end = (error: Error): void => {
      if (settled) return;
      settled = true;
      clearTimeout(stagger);
      for (const socket of trying) socket.destroy();
      reject(error);
    };
    const aborted = () => end(new Error("ECANCELED"));
    if (signal.aborted) return aborted();
    signal.addEventListener("abort", aborted, { once: true });
    const attempt = (): void => {
      clearTimeout(stagger);
      const address = order[next];
      if (settled || address === undefined) return;
      next += 1;
      const socket = dial(address, port);
      trying.add(socket);
      if (next < order.length) stagger = setTimeout(attempt, STAGGER_MS);
      socket.once("connect", () => {
        if (settled) return void socket.destroy();
        settled = true;
        clearTimeout(stagger);
        signal.removeEventListener("abort", aborted);
        for (const other of trying) if (other !== socket) other.destroy();
        resolve(socket);
      });
      socket.once("error", (error: NodeJS.ErrnoException) => {
        socket.destroy();
        trying.delete(socket);
        if (settled) return;
        failed = error.code ?? failed;
        if (next < order.length) attempt();
        else if (trying.size === 0) end(new Error(failed));
      });
    };
    if (order.length === 0) return end(new Error(failed));
    attempt();
  });
}

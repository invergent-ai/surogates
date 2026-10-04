// The sandbox for one folder (spec, Section 4): nothing readable but the system,
// the app, the folder, its temp folder and the user's toolchains; nothing
// writable but the folder and the temp folder; the network only to the package hosts.

import { lookup } from "node:dns/promises";
import { existsSync } from "node:fs";
import { BlockList, isIP } from "node:net";
import { networkInterfaces } from "node:os";
import { join } from "node:path";

import { inside } from "../files/paths.js";
import type { Destination } from "./messages.js";

import type { SandboxRuntimeConfig } from "@anthropic-ai/sandbox-runtime";

const SYSTEM = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/proc", "/sys", "/dev", "/run/systemd/resolve"];
// srt reads these in a policy path as a glob: allowRead widens, allowWrite drops the path.
export const GLOB = /[*?[\]]/;
// How deep srt's scan for nested protected names looks, from the folder: its maximum (its default is 3).
const SCAN_DEPTH = 10;
const TOOLCHAINS = [".nvm", ".pyenv", ".rustup", ".cargo/bin", ".local/bin", ".local/lib", "go", ".bun", ".deno", ".sdkman"];

// srt binds its own temp folder read-write into every sandbox, a channel shared with all the others.
const SRT_TMP = ["/tmp/claude", "/private/tmp/claude"];

// The package hosts commands may reach without asking: the cloud's list
// (surogates/tools/workspace_io/local.py) without its coding-agent endpoints.
// srt's proxy refuses every other host with a 403 until approvals exist. srt reads
// the list globally, so it is the whole host's, the file helper's included (it
// makes no network calls).
export const PACKAGE_HOSTS = [
  "github.com", "*.github.com", "*.githubusercontent.com", "pypi.org", "*.pypi.org", "files.pythonhosted.org",
  "npmjs.org", "*.npmjs.org", "registry.npmjs.org",
];

// A destination as srt compares it with allowedDomains: lower case, IPv4 shorthand
// spelled out, IPv6 in brackets, no trailing dot. srt's ask callback gives the host
// as the command spelled it. Null for one srt would not dial.
export function destination(host: string, port: number | undefined): Destination | null {
  if (port === undefined || !Number.isInteger(port) || port < 1 || port > 65_535) return null;
  const bare = host.startsWith("[") && host.endsWith("]") ? host.slice(1, -1) : host;
  let name: string;
  try {
    name = new URL(`http://${isIP(bare) === 6 ? `[${bare}]` : bare}/`).hostname.replace(/\.$/, "");
  } catch {
    return null;
  }
  // srt spells an IPv4-mapped address as the IPv4 one, which it connects to.
  const mapped = /^\[::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})\]$/.exec(name);
  if (mapped) {
    const [hi, lo] = [Number.parseInt(mapped[1] ?? "", 16), Number.parseInt(mapped[2] ?? "", 16)];
    name = `${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`;
  }
  // srt's own charset for a host, so never a pattern: an entry with a * would let a whole domain through.
  return /^([a-z0-9_-]+(\.[a-z0-9_-]+)*|\[[0-9a-f:.]+\])$/.test(name) ? { host: name, port } : null;
}

// Where a destination leads: this computer itself, a private network, or elsewhere.
export type Reach = "own" | "private" | "public";

// How long a name may take to look up before the connection is refused.
export const LOOKUP_MS = 2_000;

const subnets = (ranges: Array<[string, number]>) => {
  const list = new BlockList();
  for (const [net, prefix] of ranges) list.addSubnet(net, prefix, isIP(net) === 6 ? "ipv6" : "ipv4");
  return list;
};
// Loopback, and the unspecified address, which connects to this computer on common stacks.
const LOCAL = subnets([["127.0.0.0", 8], ["0.0.0.0", 8], ["::1", 128], ["::", 128]]);
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
const interfaceAddresses = () => Object.values(networkInterfaces()).flatMap((entries) => (entries ?? []).map((entry) => entry.address));

export interface ReachOptions {
  resolve?: (name: string) => Promise<string[]>; // a name's addresses; dns.lookup by default
  local?: () => string[]; // this computer's own addresses; its interfaces' by default, read at each call
  timeoutMs?: number;
}

/**
 * Where a destination from destination() leads. Any of a name's addresses on this
 * computer makes it "own": srt dials an allowed literal or localhost without judging
 * it again, so a command could reach the user's own services through the proxy.
 * Null when a name cannot be looked up in time. BlockList matches IPv4-mapped IPv6 too.
 */
export async function reach(host: string, options: ReachOptions = {}): Promise<Reach | null> {
  const { resolve = addressesOf, local = interfaceAddresses, timeoutMs = LOOKUP_MS } = options;
  const bare = host.startsWith("[") ? host.slice(1, -1) : host;
  let addresses: string[] = [bare];
  if (!isIP(bare)) {
    // RFC 6761: whatever a resolver says, these are this computer, and these are nowhere.
    if (bare === "localhost" || bare.endsWith(".localhost")) return "own";
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
  if (addresses.some(own)) return "own";
  return addresses.some((address) => PRIVATE.check(address, family(address))) ? "private" : "public";
}

// srt binds its own /tmp/claude into every sandbox whenever it exists, whatever
// the policy says, and other srt users (Claude Code among them) keep files there.
// A later mount wins, so an empty tmpfs goes over it, just after srt's last
// mount (0.0.77). If srt's line ever lacks that anchor, no sandbox starts
// rather than one that shows /tmp/claude.
const MOUNTS_END = " --dev /dev --unshare-pid ";
export function hideSrtTmp(line: string): string {
  const at = unquotedIndex(line, MOUNTS_END);
  if (at < 0) throw new Error("srt's sandbox command has changed: cannot hide /tmp/claude");
  return `${line.slice(0, at)} --tmpfs /tmp/claude${line.slice(at)}`;
}

// One word on a bash line: in '...', an embedded quote written as '\''.
export const quote = (text: string) => `'${text.replaceAll("'", `'\\''`)}'`;

// Where *text* first appears among the line's words, never inside a quoted word:
// srt's scan puts paths the agent named in the folder on the line, and one can hold
// the anchor. srt quotes a word in '...' and writes an embedded quote as "'".
function unquotedIndex(line: string, text: string): number {
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (ch === "'" || ch === '"') {
      const end = line.indexOf(ch, i + 1);
      if (end < 0) return -1;
      i = end;
    } else if (line.startsWith(text, i)) {
      return i;
    }
  }
  return -1;
}

// A folder the sandbox must never be given: the kernel's and the devices' folders; /run, which holds the
// user's sockets (the session bus, the agents), but not /run/media, where drives are mounted; and srt's
// temp folder, which the policy makes read-only.
export function isReserved(path: string): boolean {
  return (
    ["/proc", "/sys", "/dev"].some((dir) => inside(path, dir)) ||
    (inside(path, "/run") && !inside(path, "/run/media")) ||
    SRT_TMP.some((dir) => inside(path, dir))
  );
}

export interface PolicyInput {
  folder: string;
  tmp: string;
  home: string;
  appDirs: string[];
  bwrapPath?: string;
  socatPath?: string;
  rgPath?: string;
}

export function sandboxPolicy({ folder, tmp, home, appDirs, bwrapPath, socatPath, rgPath }: PolicyInput): SandboxRuntimeConfig {
  return {
    ...(bwrapPath ? { bwrapPath } : {}),
    ...(socatPath ? { socatPath } : {}),
    // srt's scan for nested protected names (rg, from the folder) must not read the
    // folder's .ignore, .rgignore or .gitignore: the agent writes those, and one
    // naming a nested repo would leave its .git/config writable.
    ripgrep: { command: rgPath ?? "rg", args: ["--no-ignore"] },
    mandatoryDenySearchDepth: SCAN_DEPTH,
    network: { allowedDomains: PACKAGE_HOSTS, deniedDomains: [] },
    filesystem: {
      denyRead: ["/"],
      allowRead: [
        ...SYSTEM, ...appDirs, folder, tmp,
        ...TOOLCHAINS.map((name) => join(home, name)).filter((path) => existsSync(path) && !GLOB.test(path)),
      ],
      allowWrite: [folder, tmp],
      // hideSrtTmp hides /tmp/claude under an empty tmpfs; this keeps it read-only
      // even where that tmpfs did not apply.
      denyWrite: SRT_TMP,
    },
  };
}

// macOS's side of the file helper's sandbox (srt-os.ts): srt runs the helper under /usr/bin/sandbox-exec, in a
// Seatbelt profile it generates, deny-default. It reads by layers, last match winning: everything, then the denied
// paths, then the allowed ones, so the policy's denyRead of / with its allow-list is a read allow-list, as on Linux.
// The profile has no system-socket allowance for AF_UNIX without allowUnixSockets, which the policy never sets, and
// lets the helper connect to srt's own proxy alone, which refuses every host. srt needs neither bwrap nor socat here,
// and reads no seccomp option.

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { rmSync } from "node:fs";

import { cleanly } from "../clean-child.js";

import { findOnPath } from "../files/operations.js";
import { inside } from "../files/paths.js";
import { dirname, isAbsolute, join } from "../host-path.js";
import type { SrtOs } from "./srt-os.js";

// The most bytes of a unix socket's path, without its NUL: Darwin's sun_path is 104 bytes (<sys/un.h>). srt's only
// socket on macOS is its proxy's backend, srt-mux-<pid>-<a count in base 36>.sock, in its temp folder; a pid has at
// most five digits there.
const SOCKET_MAX = 103;
const LONGEST_SOCKET = `srt-mux-${"9".repeat(5)}-${"z".repeat(4)}.sock`;

// The user's own temp folder, /var/folders/<2>/<28>/T/, private to them: confstr(3)'s _CS_DARWIN_USER_TEMP_DIR, which
// getconf(1) names DARWIN_USER_TEMP_DIR. Its path is 49 bytes, where the app's data, ~/Library/Application Support/
// Surogate, takes a socket's 104 bytes beyond a long user name. Read once, as a host starts.
let userTemp: string | null = null;
function userTempFolder(): string {
  if (userTemp !== null) return userTemp;
  const [file, args] = cleanly("/usr/bin/getconf", ["DARWIN_USER_TEMP_DIR"]);
  const read = spawnSync(file, args, { encoding: "utf8", env: {}, stdio: ["ignore", "pipe", "pipe"], timeout: 5_000 });
  const path = read.status === 0 ? read.stdout.trim().replace(/\/+$/, "") : "";
  if (!isAbsolute(path) || /[\n\r]/.test(path)) throw new Error(`this Mac did not say where its user's temp folder is: ${read.stderr.trim() || read.stdout.trim()}`);
  userTemp = path;
  return path;
}

// What the helper's sandbox lacks of this Mac, by the name a person installs it by (fileToolsMissing). Anything else
// the sandbox's line is given is what its VM lacks, in the VM's own words (requirementsDarwin): "This Mac cannot run
// Surogate's sandbox: <why>". Installing Surogate again brings back a tool, not virtualization.
const TOOLS = ["ripgrep"];
const toolsMissing = (lacking: string[]): string => {
  const tools = lacking.filter((name) => TOOLS.includes(name));
  const vm = lacking.filter((name) => !TOOLS.includes(name));
  return [...vm, ...(tools.length > 0 ? [`Surogate's sandbox tools are missing. Install Surogate again. It lacks ${tools.join(", ")}`] : [])].join(". ");
};

// The user's own temp folders, which are Linux's /tmp: /tmp itself (a link to /private/tmp) and the per-user
// folders under /private/var/folders that os.tmpdir() names. The rest of /private is the system's own.
const TEMP = ["/private/tmp", "/private/var/folders"];

export const DARWIN_SRT: SrtOs = {
  wraps: true,
  // What node, rg and the shell srt starts them through read: their programs, the system's libraries and
  // frameworks, the dyld shared cache in the OS's cryptex, the system's configuration and the time zones, and the
  // devices, as Linux's list is its programs, libraries and configuration. Never /System whole: /System/Volumes/Data
  // is the data volume, every user's home folder in it. Measured on macOS 15 (srt-darwin.test.ts), each taken out on
  // its own: node, fsat and rg start without any of them but /System/Library; the two cryptex paths name one folder.
  system: [
    "/usr", "/bin", "/sbin", "/System/Library", "/System/Cryptexes", "/System/Volumes/Preboot/Cryptexes", "/private/etc",
    "/private/var/db/timezone", "/dev",
  ],
  // The system's own folders, its devices and its crash files; /Volumes itself, where every volume is mounted, but
  // not a volume in it.
  reserved: (path) =>
    ["/System", "/Library", "/dev", "/cores"].some((dir) => inside(path, dir)) || path === "/Volumes"
    || (inside(path, "/private") && !TEMP.some((dir) => inside(path, dir))),
  toolsMissing,
  // ripgrep alone, which the helper searches with: srt on macOS runs Apple's sandbox-exec.
  fileToolsMissing: (_bwrap, path) => (findOnPath("rg", path, "/") === null ? [...TOOLS] : []),
  // The app's own rg, beside the node the host runs on (bin/ in a development build, the bundle's own folder in the
  // app), before anything of the app's PATH: a Mac's PATH has no rg, and one a person installed reads libraries
  // the sandbox does not.
  toolsPath: (outside, node) => `${dirname(node)}:${outside}`,
  // As srt gives it: its profile lets the helper's node map fsat as code, as every file it may read (measured on
  // macOS 15: Seatbelt's file-map-executable is not refused there).
  line: (line) => line,
  // In the user's own temp folder, by the app's data and the folder's key, so that two apps never share one: srt's
  // socket there takes at most 98 bytes.
  tempFolder: (dataDir, key) => join(userTempFolder(), `surogate-${createHash("sha256").update(`${dataDir}\0${key}`).digest("hex").slice(0, 16)}`),
  socketsFit: (dir) => Buffer.byteLength(join(dir, LONGEST_SOCKET)) <= SOCKET_MAX,
  // srt starts no process of its own on macOS, its proxy runs in the host itself: a host killed with the app leaves
  // its files alone.
  sweep: (dir) => rmSync(dir, { recursive: true, force: true }),
  // sandbox-exec refuses a string over 1 025 bytes in a profile, and the folder is one of its strings.
  folderBytes: 1000,
};

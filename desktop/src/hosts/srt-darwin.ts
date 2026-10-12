// macOS's side of the file helper's sandbox (srt-os.ts): srt runs the helper under /usr/bin/sandbox-exec, in a
// Seatbelt profile it generates, deny-default. It reads by layers, last match winning: everything, then the denied
// paths, then the allowed ones, so the policy's denyRead of / with its allow-list is a read allow-list, as on Linux.
// The profile has no system-socket allowance for AF_UNIX without allowUnixSockets, which the policy never sets, and
// lets the helper connect to srt's own proxy alone, which refuses every host. srt needs neither bwrap nor socat here,
// and reads no seccomp option.

import { rmSync } from "node:fs";
import { dirname, join } from "node:path";

import { findOnPath } from "../files/operations.js";
import { inside } from "../files/paths.js";
import type { SrtOs } from "./srt-os.js";

// The most bytes of a unix socket's path, without its NUL: Darwin's sun_path is 104 bytes (<sys/un.h>). srt's only
// socket on macOS is its proxy's backend, srt-mux-<pid>-<a count in base 36>.sock, in its temp folder; a pid has at
// most five digits there.
const SOCKET_MAX = 103;
const LONGEST_SOCKET = `srt-mux-${"9".repeat(5)}-${"z".repeat(4)}.sock`;

// The user's own temp folders, which are Linux's /tmp: /tmp itself (a link to /private/tmp) and the per-user
// folders under /private/var/folders that os.tmpdir() names. The rest of /private is the system's own.
const TEMP = ["/private/tmp", "/private/var/folders"];

export const DARWIN_SRT: SrtOs = {
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
  toolsMissing: (lacking) => `Surogate's sandbox tools are missing. Install Surogate again. It lacks ${lacking.join(", ")}`,
  // ripgrep alone, which the helper searches with: srt on macOS runs Apple's sandbox-exec.
  fileToolsMissing: (_bwrap, path) => (findOnPath("rg", path, "/") === null ? ["ripgrep"] : []),
  // The app's own rg, beside the node the host runs on (bin/ in a development build, the bundle's own folder in the
  // app), before anything of the app's PATH: a Mac's PATH has no rg, and one a person installed reads libraries
  // the sandbox does not.
  toolsPath: (outside) => `${dirname(process.execPath)}:${outside}`,
  // As srt gives it: its profile lets the helper's node map fsat as code, as every file it may read (measured on
  // macOS 15: Seatbelt's file-map-executable is not refused there).
  line: (line) => line,
  socketsFit: (dir) => Buffer.byteLength(join(dir, LONGEST_SOCKET)) <= SOCKET_MAX,
  // srt starts no process of its own on macOS, its proxy runs in the host itself: a host killed with the app leaves
  // its files alone.
  sweep: (dir) => rmSync(dir, { recursive: true, force: true }),
  // sandbox-exec refuses a string over 1 025 bytes in a profile, and the folder is one of its strings.
  folderBytes: 1000,
};

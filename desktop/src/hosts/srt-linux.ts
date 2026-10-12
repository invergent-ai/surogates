// Linux's side of the file helper's sandbox (srt-os.ts): srt runs the helper in bubblewrap, behind
// socat's bridges, under seccomp's unix-socket filter.

import { readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { join } from "node:path";

import { findOnPath } from "../files/operations.js";
import { inside } from "../files/paths.js";
import type { SrtOs } from "./srt-os.js";

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

// A unix socket's path holds at most 107 bytes; srt's longest name here is claude-socks-<16 hex>.sock.
const SOCKET_MAX = 107;
const LONGEST_SOCKET = `claude-socks-${"0".repeat(16)}.sock`;

export const LINUX_SRT: SrtOs = {
  system: ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/proc", "/sys", "/dev", "/run/systemd/resolve"],
  // The kernel's and the devices' folders; /run, which holds the user's sockets (the session bus, the agents), but
  // not /run/media, where drives are mounted.
  reserved: (path) => ["/proc", "/sys", "/dev"].some((dir) => inside(path, dir)) || (inside(path, "/run") && !inside(path, "/run/media")),
  // Section 9's words for a computer that lacks what the sandbox runs on, and what it lacks.
  toolsMissing: (lacking) => `Surogate's sandbox tools are missing. Run the install script again. It lacks ${lacking.join(", ")}`,
  // Each named as the install script installs it: bubblewrap, at *bwrap* (the installed version's own copy) or else
  // on *path*; socat, which srt runs; and ripgrep, which the helper searches with.
  fileToolsMissing: (bwrap, path) => [["bubblewrap", bwrap ?? "bwrap"], ["socat", "socat"], ["ripgrep", "rg"]]
    .filter(([, program]) => findOnPath(program!, path, "/") === null).map(([name]) => name!),
  toolsPath: (outside) => outside,
  line: hideSrtTmp,
  // In the app's data, by the folder's key.
  tempFolder: (dataDir, key) => join(dataDir, "srt", key),
  socketsFit: (dir) => Buffer.byteLength(join(dir, LONGEST_SOCKET)) <= SOCKET_MAX,
  // What a host killed together with the app left of srt: its bridges (socat, whose command line names their
  // socket), then its files.
  sweep(dir) {
    for (const pid of readdirSync("/proc").filter((name) => /^\d+$/.test(name))) {
      try {
        // The user's own only: reading a command line waits on that process's memory, which one stuck in the kernel holds.
        if (statSync(`/proc/${pid}`).uid !== process.getuid?.()) continue;
        if (readFileSync(`/proc/${pid}/cmdline`, "latin1").includes(`${dir}/`)) process.kill(Number(pid), "SIGKILL");
      } catch {
        // Gone, or not ours to read.
      }
    }
    rmSync(dir, { recursive: true, force: true });
  },
  folderBytes: null,
};

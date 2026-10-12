// How the browser host looks at this computer's processes and temporary folder on Linux: the
// processes on a profile by their command lines in /proc, and the short folder for the browser's
// own temporary files.

import { readdir, readFile } from "node:fs/promises";

// How long a look at this computer's processes waits with none of its reads answering.
const SCAN_MS = 1_000;
// ponytail: the command lines that did not come while no other did for SCAN_MS, for the host's life: a
// process blocked in the kernel (as on a dead mount) blocks its readers too, and each such read holds one
// of libuv's four threads until it returns. They are not asked again.
const unread = new Set<string>();

/**
 * The processes on *profile*, by pid: each names it on its command line. None where /proc is not.
 * Read off the event loop, and passed over where one does not come: once SCAN_MS has gone by with no
 * read answering. Not once SCAN_MS has gone by since the look began: the reads queue on libuv's threads,
 * and on a busy computer one late in that queue has not come only because it waits its turn.
 */
export async function holding(profile: string, read = (path: string) => readFile(path, "utf8")): Promise<number[]> {
  const named = [`${profile}\0`, `${profile}/`, `${profile} `];
  const pids = (await readdir("/proc").catch(() => [] as string[])).filter((pid) => /^\d+$/.test(pid));
  const found: number[] = [];
  const waiting = new Set<string>();
  await new Promise<void>((resolve) => {
    let timer: NodeJS.Timeout | undefined;
    let over = false;
    const end = () => {
      over = true;
      clearTimeout(timer);
      resolve();
    };
    // Each answer gives what is left SCAN_MS more; with nothing left, the look is over.
    const answered = () => {
      if (over) return;
      clearTimeout(timer);
      if (waiting.size === 0) return end();
      timer = setTimeout(end, SCAN_MS);
    };
    for (const path of pids.map((pid) => `/proc/${pid}/cmdline`).filter((path) => !unread.has(path))) {
      waiting.add(path);
      void read(path).then((line) => {
        if (!over && named.some((name) => line.includes(name))) found.push(Number(path.split("/")[2]));
      }, () => {}).finally(() => {
        waiting.delete(path);
        answered();
      });
    }
    answered();
  });
  for (const path of waiting) unread.add(path);
  return found.sort((a, b) => a - b);
}

// The browser's own temp folder holds its singleton's socket, whose path a Unix socket keeps
// under 108 bytes: the user's runtime folder, else /tmp.
export const browserTemp = (env: NodeJS.ProcessEnv): string => env.XDG_RUNTIME_DIR || "/tmp";

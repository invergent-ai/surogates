// What a file host was handed beyond what it is meant to have, closed at its first line.
//
// The app starts it with its three standard descriptors and its channel and nothing else
// (clean-child.ts). Should a caller forget: whatever of the app's main process it was started with
// would be this process's, and its children's, the sandboxed helper among them. Node marks what it
// inherits close-on-exec only up to the first number above 15 that is not open, and Chromium's are
// not in a row. So every descriptor above the standard three that is not close-on-exec is closed:
// Node's own, the channel among them, all are.

import { closeSync, readdirSync, readFileSync } from "node:fs";

const CLOEXEC = 0o2000000;

/** Closes each descriptor above 2 that a child of this process would inherit. How many it closed. */
export function closeHanded(): number {
  let closed = 0;
  for (const name of readdirSync("/proc/self/fd")) {
    const fd = Number(name);
    if (fd <= 2) continue;
    try {
      const flags = Number.parseInt(/^flags:\s*([0-7]+)$/m.exec(readFileSync(`/proc/self/fdinfo/${fd}`, "utf8"))?.[1] ?? "", 8);
      if (Number.isNaN(flags) || (flags & CLOEXEC) !== 0) continue;
      closeSync(fd);
      closed += 1;
    } catch {
      // The listing's own descriptor, closed since.
    }
  }
  return closed;
}

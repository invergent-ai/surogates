// The app's own small files under its state root: each read whole, and written whole.

import { existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";

/** *path*'s JSON, or *fallback* when there is none. A file that cannot be read is said, then read as *fallback*. */
export function readState<T>(path: string, fallback: T, onError: (error: Error) => void = console.warn): T {
  if (!existsSync(path)) return fallback;
  try {
    return JSON.parse(readFileSync(path, "utf8")) as T;
  } catch (error) {
    onError(new Error(`${path} could not be read, so Surogate starts without it: ${error instanceof Error ? error.message : String(error)}`));
    return fallback;
  }
}

/** Write *value* to *path* whole or not at all; with *mode*, never readable by anyone else, even for a moment. */
export function writeState(path: string, value: unknown, mode?: number): void {
  const next = `${path}.${process.pid}.tmp`;
  writeFileSync(next, `${JSON.stringify(value, null, 2)}\n`, mode === undefined ? {} : { mode });
  renameSync(next, path);
}

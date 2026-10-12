// macOS's folder lock: a file of the app's own, <data>/locks/<uid>-<dev>-<ino>, opened with O_EXLOCK | O_NONBLOCK,
// which takes its flock in the open itself or fails at once (open(2): "O_EXLOCK atomically obtain an exclusive lock").
// The kernel lets go of it when the descriptor closes, as it does when its holder dies, however it dies. macOS has no
// abstract socket namespace for Linux's way. The file is never removed: a lock taken on a file another just removed
// would be no one's.

import { closeSync, constants, mkdirSync, openSync } from "node:fs";
import { join } from "node:path";

import { fsat } from "../files/fsat.js";
import type { TakeLock } from "./folder-lock.js";

export const takeDarwinLock: TakeLock = async (uid, dev, ino, dataDir) => {
  if (dataDir === undefined) throw new Error("the folder lock on macOS is a file in the app's data folder, and none was given");
  const dir = join(dataDir, "locks");
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const { O_EXLOCK } = fsat().constants;
  let fd: number;
  try {
    // Close-on-exec, as Node opens every file: the host's helper never holds it.
    fd = openSync(join(dir, `${uid}-${dev}-${ino}`), constants.O_CREAT | constants.O_RDWR | O_EXLOCK | constants.O_NONBLOCK | constants.O_NOFOLLOW, 0o600);
  } catch (error) {
    // Another holds it: Darwin's EWOULDBLOCK, which is its EAGAIN.
    if ((error as NodeJS.ErrnoException).code === "EAGAIN") return null;
    throw error;
  }
  return { close: () => closeSync(fd) };
};

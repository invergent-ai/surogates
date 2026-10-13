// Windows's folder lock: a named pipe, \\.\pipe\surogate-folder-<the user's SID>-<dev>-<ino>, listened on with Node's
// net, the Windows form of Linux's abstract socket. libuv makes a pipe's first instance alone
// (FILE_FLAG_FIRST_PIPE_INSTANCE: "If you attempt to create multiple instances of a pipe with this flag, creation of
// the first instance succeeds, but creation of the next instance fails with ERROR_ACCESS_DENIED"), so a second host's
// listen fails while the first holds it, and the pipe goes when its holder dies, however it dies. Another user of the
// computer can take the name first and stall the host, as on Linux; the name carries the user's SID, so no two users'
// hosts share one.

import { createServer } from "node:net";

import { ntat } from "../files/ntat.js";
import type { TakeLock } from "./folder-lock.js";

// The user's SID, read once: it names this process's user for its life.
let sid: string | null = null;

/** The pipe that is the lock of the folder *dev*:*ino* for this process's user. */
export const lockPipe = (dev: number, ino: number): string => `\\\\.\\pipe\\surogate-folder-${(sid ??= ntat().userSid())}-${dev}-${ino}`;

export const takeWin32Lock: TakeLock = async (_uid, dev, ino) => {
  // Anything on the computer may connect to a pipe's name: drop it at once.
  const server = createServer((socket) => socket.destroy());
  try {
    await new Promise<void>((resolve, reject) => {
      // on, not once: a later error lands on the settled promise, not on the host.
      server.on("error", reject);
      server.listen(lockPipe(dev, ino), resolve);
    });
  } catch (error) {
    // libuv's answer for a name another instance holds: uv_pipe_bind takes the first instance's ERROR_ACCESS_DENIED to
    // EADDRINUSE. Any other failure is the host's to see.
    if ((error as NodeJS.ErrnoException).code === "EADDRINUSE") return null;
    throw error;
  }
  // It holds the name; it does not keep the host running.
  server.unref();
  return server;
};

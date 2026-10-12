// Linux's folder lock: a name in the kernel's abstract socket namespace, which one process at a time can hold and the
// kernel frees when that process dies. Each sandbox has its own network namespace, so no command can take it.

import { createServer } from "node:net";

import type { TakeLock } from "./folder-lock.js";

export const takeLinuxLock: TakeLock = async (uid, dev, ino) => {
  const name = `\0surogate-folder-${uid}-${dev}-${ino}`;
  // Anything in the host's network namespace can connect to the name: drop it at once.
  const server = createServer((socket) => socket.destroy());
  try {
    await new Promise<void>((resolve, reject) => {
      // on, not once: a later error lands on the settled promise, not on the host.
      server.on("error", reject);
      server.listen(name, resolve);
    });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EADDRINUSE") return null;
    throw error;
  }
  // It holds the name; it does not keep the host running.
  server.unref();
  return server;
};

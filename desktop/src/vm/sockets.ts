// The Unix sockets a backend's hypervisor makes in the VM's runtime folder as it starts, which the
// VM manager connects to: the guest's control, net and inbound ports, and on Linux QEMU's monitor.

import type { ChildProcess } from "node:child_process";
import { connect, type Socket } from "node:net";

// How long a socket that is not there yet is waited for before it is tried again.
export const RETRY_MS = 5;

export const ended = (child: ChildProcess) => child.exitCode !== null || child.signalCode !== null;

// A socket the hypervisor *child* makes as it starts, as QEMU does, tried until it takes the
// connection; null once *child* has exited, or at *deadline*.
export async function reach(path: string, child: ChildProcess, deadline: number): Promise<Socket | null> {
  while (!ended(child) && performance.now() <= deadline) {
    const socket = await new Promise<Socket | null>((resolve) => {
      const tried = connect(path);
      tried.once("connect", () => resolve(tried));
      tried.once("error", () => setTimeout(() => resolve(null), RETRY_MS));
    });
    if (socket) return socket;
  }
  return null;
}

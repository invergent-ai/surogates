// The folders this computer's chats are bound to, as the app holds them for its PATH rule
// (pathOutside): each by what it is now, looked at off the main process's own thread.
//
// A look into a network or FUSE mount whose server has gone does not return until the mount does.
// On the main thread it would stop the tray, the pages and the device's link for as long. So each
// look is fs.promises' and has a bound. Node cannot take a look back, and one that does not answer
// holds one of libuv's four threads, which the process's other file calls wait on: so the process
// has LOOKS_AT_ONCE looks running, whoever asked for them, and one look at a folder at a time,
// which everyone who asks for that folder shares.

import type { Stats } from "node:fs";
import { stat } from "node:fs/promises";

/** How long a folder has to answer. */
export const LOOK_MS = 5_000;
const LOOKS_AT_ONCE = 2;

type Found = { dev: number; ino: number } | null;

export class FolderLooks {
  // The looks that have not answered yet, each by its folder, with when it began.
  private readonly running = new Map<string, { answer: Promise<Found>; began: number }>();
  // Those waiting for one of them to end.
  private waiting: Array<() => void> = [];

  /** *said*: told once of each folder whose look did not answer in its time, for the log. */
  constructor(private readonly look: (folder: string) => Promise<Stats> = stat, private readonly ms = LOOK_MS, private readonly said: (folder: string) => void = () => {}) {}

  /**
   * What each of *folders* is now. One that is not there, cannot be read, or has not answered within
   * the bound is not held: its file host cannot start either, so nothing is run from it. A folder
   * whose look cannot begin yet, because as many as the process may have are running, waits for one
   * of them to end. It is given up, and not held, only where every one of those has been silent for
   * longer than its bound: no look ends then, and another would take one more of the app's threads.
   */
  async held(folders: string[]): Promise<Array<{ dev: number; ino: number }>> {
    const found = await Promise.all([...new Set(folders)].map((folder) => this.one(folder)));
    return found.filter((id) => id !== null);
  }

  // What *folder* is, by the look at it that runs, or by one begun once there is room for it.
  private async one(folder: string): Promise<Found> {
    for (;;) {
      const shared = this.running.get(folder);
      if (shared) return this.bounded(shared);
      if (this.running.size < LOOKS_AT_ONCE) break;
      const now = Date.now();
      // How long each running look still has to answer in. Where none has any time left, none
      // will end: given up. Else waited for, to the soonest that one ends or runs out of time.
      const left = [...this.running.values()].map(({ began }) => began + this.ms - now).filter((ms) => ms > 0);
      if (left.length === 0) return null;
      const soonest = Math.min(...left);
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, soonest).unref();
        this.waiting.push(() => (clearTimeout(timer), resolve()));
      });
    }
    const began = Date.now();
    const answer = this.look(folder).then(({ dev, ino }) => ({ dev, ino }), () => null).finally(() => {
      this.running.delete(folder);
      const waiting = this.waiting;
      this.waiting = [];
      for (const wake of waiting) wake();
    });
    const begun = { answer, began };
    this.running.set(folder, begun);
    const timer = setTimeout(() => {
      if (this.running.get(folder) === begun) this.said(folder);
    }, this.ms).unref();
    void answer.finally(() => clearTimeout(timer));
    return this.bounded(begun);
  }

  // A look's answer, or null once the look is older than its bound: at once where it is already.
  private bounded({ answer, began }: { answer: Promise<Found>; began: number }): Promise<Found> {
    const left = began + this.ms - Date.now();
    if (left <= 0) return Promise.resolve(null);
    let timer: NodeJS.Timeout | undefined;
    return Promise.race([answer, new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), left).unref();
    })]).finally(() => clearTimeout(timer));
  }
}

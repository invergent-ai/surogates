// The folders this computer's chats are bound to, as the app holds them for its PATH rule
// (pathOutside): each by what it is now, looked at off the main process's own thread.
//
// A look into a network or FUSE mount whose server has gone does not return until the mount does.
// On the main thread it would stop the tray, the pages and the device's link for as long. So each
// look is fs.promises' and has a bound. Node cannot take a look back, and one that does not answer
// holds one of libuv's four threads, which the process's other file calls wait on: so a folder is
// looked at once at a time, and the process looks at LOOKS_AT_ONCE.

import type { Stats } from "node:fs";
import { stat } from "node:fs/promises";

/** How long a folder has to answer. */
export const LOOK_MS = 5_000;
const LOOKS_AT_ONCE = 2;

export class FolderLooks {
  // The looks that have not answered yet, each by its folder.
  private readonly silent = new Set<string>();

  constructor(private readonly look: (folder: string) => Promise<Stats> = stat, private readonly ms = LOOK_MS) {}

  /**
   * What each of *folders* is now. One that is not there, cannot be read, or has not answered within
   * the bound is not held: its file host cannot start either, so nothing is run from it. Nor is one
   * whose look could not begin, because as many as the process may have are still silent.
   */
  async held(folders: string[]): Promise<Array<{ dev: number; ino: number }>> {
    const distinct = [...new Set(folders)];
    const found = new Array<{ dev: number; ino: number } | null>(distinct.length).fill(null);
    let next = 0;
    const one = async (): Promise<void> => {
      for (let at = next++; at < distinct.length; at = next++) {
        const folder = distinct[at]!;
        if (this.silent.has(folder) || this.silent.size >= LOOKS_AT_ONCE) continue;
        this.silent.add(folder);
        const answered = this.look(folder).then(({ dev, ino }) => ({ dev, ino }), () => null).finally(() => this.silent.delete(folder));
        let timer: NodeJS.Timeout | undefined;
        found[at] = await Promise.race([answered, new Promise<null>((resolve) => {
          timer = setTimeout(() => resolve(null), this.ms).unref();
        })]).finally(() => clearTimeout(timer));
      }
    };
    await Promise.all(Array.from({ length: LOOKS_AT_ONCE }, one));
    return found.filter((id) => id !== null);
  }
}

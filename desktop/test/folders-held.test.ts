// The folders the app holds for its PATH rule, looked at off the main thread: each under a bound, a
// few at once, and one that does not answer neither held nor looked at again while it is silent.

import type { Stats } from "node:fs";
import { mkdtempSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FolderLooks } from "../src/shell/folders-held.js";

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "held-"));
});
afterEach(() => rmSync(dir, { recursive: true, force: true }));

describe("the folders the app holds", () => {
  it("are each by what the folder is now, and one that is not there is not held", async () => {
    const { dev, ino } = statSync(dir);
    expect(await new FolderLooks().held([dir, join(dir, "gone"), dir])).toEqual([{ dev, ino }]);
  });

  it("do not wait for a folder that never answers: it is not held, the others are, and it is not looked at again while it is silent", async () => {
    const asked: string[] = [];
    let answer: ((found: Stats) => void) | undefined;
    const look = (folder: string) => {
      asked.push(folder);
      return folder === "/dead" ? new Promise<Stats>((resolve) => (answer = resolve)) : Promise.resolve({ dev: 1, ino: folder.length } as Stats);
    };
    const looks = new FolderLooks(look, 50);
    const began = Date.now();
    expect(await looks.held(["/dead", "/a", "/bb"])).toEqual([{ dev: 1, ino: 2 }, { dev: 1, ino: 3 }]);
    expect(Date.now() - began).toBeLessThan(1_000);
    // Node cannot take a look back: a second one into the same mount would hold a second thread.
    expect(await looks.held(["/dead", "/a"])).toEqual([{ dev: 1, ino: 2 }]);
    expect(asked).toEqual(["/dead", "/a", "/bb", "/a"]);
    // Once it has answered, it is looked at again like any other.
    answer!({ dev: 9, ino: 9 } as Stats);
    await new Promise((resolve) => setTimeout(resolve, 5));
    asked.length = 0;
    const late = looks.held(["/dead"]);
    answer!({ dev: 9, ino: 9 } as Stats);
    expect([await late, asked]).toEqual([[{ dev: 9, ino: 9 }], ["/dead"]]);
  });

  it("are looked at two at a time, and with two that do not answer no other is looked at: the threads left are the app's", async () => {
    let running = 0;
    let most = 0;
    const slow = async (folder: string) => {
      most = Math.max(most, ++running);
      await new Promise((resolve) => setTimeout(resolve, 10));
      running -= 1;
      return { dev: 2, ino: Number(folder.slice(1)) } as Stats;
    };
    expect(await new FolderLooks(slow, 1_000).held(["/1", "/2", "/3", "/4", "/5"])).toEqual([1, 2, 3, 4, 5].map((ino) => ({ dev: 2, ino })));
    expect(most).toBe(2);
    const asked: string[] = [];
    const dead = new FolderLooks((folder) => (asked.push(folder), folder.startsWith("/dead") ? new Promise<Stats>(() => {}) : Promise.resolve({ dev: 3, ino: 3 } as Stats)), 20);
    expect(await dead.held(["/dead1", "/dead2", "/alive"])).toEqual([]);
    expect(asked).toEqual(["/dead1", "/dead2"]);
  });
});

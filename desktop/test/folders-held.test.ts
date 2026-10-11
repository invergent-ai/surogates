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

  it("are looked at two at a time by all calls together, and each folder once for all who ask: a second call at once holds what the first holds", async () => {
    let running = 0;
    let most = 0;
    const asked: string[] = [];
    const slow = async (folder: string) => {
      asked.push(folder);
      most = Math.max(most, ++running);
      await new Promise((resolve) => setTimeout(resolve, 10));
      running -= 1;
      return { dev: 2, ino: Number(folder.slice(1)) } as Stats;
    };
    const all = [1, 2, 3, 4, 5].map((ino) => ({ dev: 2, ino }));
    const folders = ["/1", "/2", "/3", "/4", "/5"];
    const looks = new FolderLooks(slow, 1_000);
    // Two calls in one turn, as a click on Check again makes: both hold every folder, by one look at each.
    expect(await Promise.all([looks.held(folders), looks.held(folders)])).toEqual([all, all]);
    expect([most, asked]).toEqual([2, folders]);
    // And a third while they run, for one folder more.
    const [first, third] = await Promise.all([looks.held(folders), looks.held(["/6", "/1"])]);
    expect([first, third, most]).toEqual([all, [{ dev: 2, ino: 6 }, { dev: 2, ino: 1 }], 2]);
  });

  it("hold every folder that answers beside one that never does, at the first call and at each after it: the dead one has one of the two looks, and the other serves the rest", async () => {
    const asked: string[] = [];
    const said: string[] = [];
    const look = (folder: string) => (asked.push(folder), folder === "/dead" ? new Promise<Stats>(() => {}) : Promise.resolve({ dev: 3, ino: Number(folder.slice(1)) } as Stats));
    const looks = new FolderLooks(look, 300, (folder) => said.push(folder));
    const healthy = [1, 2, 3, 4].map((ino) => ({ dev: 3, ino }));
    const folders = ["/dead", "/1", "/2", "/3", "/4"];
    expect(await looks.held(folders)).toEqual(healthy);
    for (let again = 0; again < 3; again += 1) {
      const began = Date.now();
      expect(await looks.held(folders)).toEqual(healthy);
      // The dead one is not waited for a second time: its look is older than its bound.
      expect(Date.now() - began).toBeLessThan(250);
    }
    expect(asked.filter((folder) => folder === "/dead")).toEqual(["/dead"]);
    // Said once, to the log: its chat's tools fail later, at its file host's start, and nothing else tells why.
    expect(said).toEqual(["/dead"]);
  });

  it("say a folder that did not answer in its time by when it is left out, also where its look took a moment to begin, and to each who asks after", async () => {
    const said: string[] = [];
    // A look that takes two milliseconds to begin, as one on a busy computer does: the clock moves on before it is under way.
    const look = (folder: string) => {
      const until = Date.now() + 2;
      while (Date.now() < until) {}
      return folder === "/dead" ? new Promise<Stats>(() => {}) : Promise.resolve({ dev: 4, ino: 1 } as Stats);
    };
    const looks = new FolderLooks(look, 50, (folder) => said.push(folder));
    expect(await looks.held(["/dead"])).toEqual([]);
    expect(said).toEqual(["/dead"]);
    expect(await looks.held(["/dead", "/1"])).toEqual([{ dev: 4, ino: 1 }]);
    expect(said).toEqual(["/dead"]);
  });

  it("begin no third look while two have not answered within their bound: the threads left are the app's, and a folder that would have answered is not held", async () => {
    const asked: string[] = [];
    const dead = new FolderLooks((folder) => (asked.push(folder), folder.startsWith("/dead") ? new Promise<Stats>(() => {}) : Promise.resolve({ dev: 3, ino: 3 } as Stats)), 20);
    const began = Date.now();
    // It waits for a look to end for as long as either may still answer, and no longer.
    expect(await dead.held(["/dead1", "/dead2", "/alive"])).toEqual([]);
    expect(Date.now() - began).toBeLessThan(500);
    expect(await dead.held(["/alive"])).toEqual([]);
    expect(asked).toEqual(["/dead1", "/dead2"]);
  });
});

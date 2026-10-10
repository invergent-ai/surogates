// What the sidebar says while a thread's copy of its folder is being made.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Copying, SAID_AFTER_MS } from "../src/shell/copying.js";

const ONE = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const TWO = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
const THREE = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f";

let changes: number;
let copying: Copying;

beforeEach(() => {
  vi.useFakeTimers();
  changes = 0;
  copying = new Copying(() => void (changes += 1));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the sidebar's line while a thread's copy is made", () => {
  it("says a copy is being made once it has taken a moment, and nothing once it has ended", () => {
    copying.heard({ root: ONE, folder: "/home/u/Reports", state: "begun" });
    expect([copying.line(), changes]).toEqual([null, 0]);
    vi.advanceTimersByTime(SAID_AFTER_MS);
    expect([copying.line(), changes]).toEqual(["Making a copy of /home/u/Reports for a thread to work in", 1]);
    copying.heard({ root: ONE, folder: "/home/u/Reports", state: "ended" });
    expect([copying.line(), changes]).toEqual([null, 2]);
  });

  it("says nothing of a copy the guest only looked at, which ended before a moment", () => {
    copying.heard({ root: ONE, folder: "/home/u/Reports", state: "begun" });
    vi.advanceTimersByTime(SAID_AFTER_MS - 1);
    copying.heard({ root: ONE, folder: "/home/u/Reports", state: "ended" });
    vi.advanceTimersByTime(SAID_AFTER_MS * 2);
    expect([copying.line(), changes]).toEqual([null, 0]);
  });

  it("says how many threads' copies of a folder are being made, and of how many folders", () => {
    copying.heard({ root: ONE, folder: "/home/u/Reports", state: "begun" });
    copying.heard({ root: TWO, folder: "/home/u/Reports", state: "begun" });
    vi.advanceTimersByTime(SAID_AFTER_MS);
    expect(copying.line()).toBe("Making copies of /home/u/Reports for 2 threads to work in");
    copying.heard({ root: THREE, folder: "/home/u/Photos", state: "begun" });
    vi.advanceTimersByTime(SAID_AFTER_MS);
    expect(copying.line()).toBe("Making copies of 2 folders for threads to work in");
    for (const root of [ONE, TWO]) copying.heard({ root, folder: "/home/u/Reports", state: "ended" });
    expect(copying.line()).toBe("Making a copy of /home/u/Photos for a thread to work in");
  });

  it("takes a making begun again for a thread as the one under way, and an end it never heard begin as nothing", () => {
    copying.heard({ root: ONE, folder: "/home/u/Reports", state: "ended" });
    copying.heard({ root: ONE, folder: "/home/u/Reports", state: "begun" });
    vi.advanceTimersByTime(SAID_AFTER_MS);
    copying.heard({ root: ONE, folder: "/home/u/Reports", state: "begun" });
    expect(copying.line()).toBe("Making a copy of /home/u/Reports for a thread to work in");
    copying.heard({ root: ONE, folder: "/home/u/Reports", state: "ended" });
    vi.advanceTimersByTime(SAID_AFTER_MS);
    expect(copying.line()).toBeNull();
  });
});

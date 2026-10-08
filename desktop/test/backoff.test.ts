import { expect, it } from "vitest";

import { Backoff } from "../src/vm/backoff.js";

it("waits 1 s after the first start that went, doubling to 60 s, and from 1 s again once one stayed up 30 s", () => {
  let now = 0;
  const backoff = new Backoff(() => now);
  expect(backoff.wait).toBe(0);
  const waits: number[] = [];
  for (let n = 0; n < 8; n += 1) {
    backoff.down();
    waits.push(backoff.wait);
    now += backoff.wait;
  }
  expect(waits).toEqual([1_000, 2_000, 4_000, 8_000, 16_000, 32_000, 60_000, 60_000]);
  // Up, but not for 30 s: the next wait doubles on.
  backoff.up();
  now += 29_999;
  backoff.down();
  expect(backoff.wait).toBe(60_000);
  now += 60_000;
  backoff.up();
  now += 30_000;
  backoff.down();
  expect(backoff.wait).toBe(1_000);
});

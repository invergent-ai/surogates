import { describe, expect, it } from "vitest";

import { ago } from "../src/shell/pages/ui.js";

const NOW = Date.parse("2026-10-06T12:00:00Z");

describe("an age", () => {
  it.each([
    ["2026-10-06T11:59:30Z", "now", "just now"],
    ["2026-10-06T11:43:00Z", "17m", "17 minutes ago"],
    ["2026-10-06T03:00:00Z", "9h", "9 hours ago"],
    ["2026-10-04T12:00:00Z", "2d", "2 days ago"],
    ["2026-10-07T12:00:00Z", "now", "just now"],
  ])("of %s is %s, or %s", (when, short, long) => {
    expect(ago(when, NOW)).toBe(short);
    expect(ago(when, NOW, "long")).toBe(long);
  });
});

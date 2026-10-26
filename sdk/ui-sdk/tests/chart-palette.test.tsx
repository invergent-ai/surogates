// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A chart's colours from the palette: a colour follows its series by its
// place among the series, a series marked other takes the neutral, and a
// fifth series that is not marked other is refused.
import { describe, expect, it } from "vitest";

import { seriesConfig } from "../src/chart-palette";

describe("chart palette", () => {
  it("series take the palette's colours by their place among the series, and the rest its neutral", () => {
    expect(
      seriesConfig([
        { key: "agent", label: "Agent working" },
        { key: "system", label: "Systems", other: true },
        { key: "staff", label: "Staff" },
      ]),
    ).toEqual({
      agent: { label: "Agent working", color: "var(--chart-series-1)" },
      system: { label: "Systems", color: "var(--chart-series-other)" },
      staff: { label: "Staff", color: "var(--chart-series-2)" },
    });
  });

  it("a fifth series not marked other is refused", () => {
    const five = ["a", "b", "c", "d", "e"].map((key) => ({ key, label: key }));

    expect(() => seriesConfig(five)).toThrow(RangeError);
    expect(() => seriesConfig(five)).toThrow(
      "The chart palette holds 4 series; mark the rest as other.",
    );
  });
});

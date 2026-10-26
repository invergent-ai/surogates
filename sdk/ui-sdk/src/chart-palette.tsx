// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The colours a chart draws its series in: four categorical colours in a
// fixed order and a neutral for the rest, each defined for both themes in
// tokens.css. A colour follows its series, never its rank: the first series
// is always the first colour, wherever the chart sorts it. A fifth series
// folds into "other", the neutral: the caller marks it `other`, and
// seriesConfig refuses an unmarked fifth. Colour-blind readers tell each
// colour from the next in the order, not every pair: tokens.css names them.

import type { ReactNode } from "react";

import type { ChartConfig } from "./chart";

/** The four categorical colours, in the order series take them. */
export const CHART_SERIES = [
  "var(--chart-series-1)",
  "var(--chart-series-2)",
  "var(--chart-series-3)",
  "var(--chart-series-4)",
] as const;

/** The neutral for the series that fold into "other". */
export const CHART_OTHER = "var(--chart-series-other)";

export type ChartSeries = {
  /** The key the chart's data carries this series under. */
  key: string;
  label: ReactNode;
  /** Draws the series in the neutral and keeps it off the four colours. */
  other?: boolean;
};

/**
 * A chart's config from its series: each series not marked `other` takes the
 * palette's colour by its place among those, each `other` takes the neutral.
 * A fifth series not marked `other` throws.
 */
export function seriesConfig(series: readonly ChartSeries[]): ChartConfig {
  const config: ChartConfig = {};
  let place = 0;
  for (const { key, label, other } of series) {
    if (other) {
      config[key] = { label, color: CHART_OTHER };
      continue;
    }
    if (place >= CHART_SERIES.length) {
      throw new RangeError(
        `The chart palette holds ${CHART_SERIES.length} series; mark the rest as other.`,
      );
    }
    config[key] = { label, color: CHART_SERIES[place] };
    place += 1;
  }
  return config;
}

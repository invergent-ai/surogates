// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Chart artifact renderer — embeds a Chart.js configuration object.
// Respects the app's dark/light theme and resizes with the thread column.

import { useEffect, useMemo, useRef, useState } from "react";
import { Chart as ChartJS } from "chart.js/auto";
import type { ChartConfiguration, ChartOptions } from "chart.js";
import { useTheme } from "next-themes";
import { cn } from "../../../lib/utils";
import type { ChartArtifactSpec } from "../../../types";

const DEFAULT_CHART_HEIGHT = 320;
const MAX_INLINE_CHART_HEIGHT = 800;

const LIGHT_THEME = {
  text: "#444",
  title: "#111",
  grid: "#e5e7eb",
  border: "#d1d5db",
};

const DARK_THEME = {
  text: "#cbd5e1",
  title: "#f1f5f9",
  grid: "#374151",
  border: "#4b5563",
};

export function ArtifactChart({
  spec,
  fill = false,
}: {
  spec: ChartArtifactSpec;
  fill?: boolean;
}) {
  const { resolvedTheme } = useTheme();
  const isDark = resolvedTheme === "dark";
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<ChartJS | null>(null);
  const [error, setError] = useState<string | null>(null);

  const config = useMemo<ChartConfiguration>(() => {
    const base = cloneChartConfig(spec.chart_js ?? {});
    return {
      ...base,
      options: mergeThemeOptions(base.options, base.type, isDark),
    } as ChartConfiguration;
  }, [spec.chart_js, isDark]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    chartRef.current?.destroy();
    chartRef.current = null;
    setError(null);

    try {
      chartRef.current = new ChartJS(canvas, config);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }

    return () => {
      chartRef.current?.destroy();
      chartRef.current = null;
    };
  }, [config]);

  return (
    <div className="flex flex-col gap-2">
      <div
        className={cn("relative w-full", fill && "h-full min-h-80")}
        style={
          fill
            ? undefined
            : {
                height: DEFAULT_CHART_HEIGHT,
                maxHeight: MAX_INLINE_CHART_HEIGHT,
              }
        }
      >
        <canvas ref={canvasRef} />
      </div>
      {error && (
        <p className="text-xs text-destructive">Chart error: {error}</p>
      )}
      {spec.caption && (
        <p className="text-xs text-muted-foreground">{spec.caption}</p>
      )}
    </div>
  );
}

function cloneChartConfig(
  value: Record<string, unknown>,
): Record<string, unknown> {
  return JSON.parse(JSON.stringify(value)) as Record<string, unknown>;
}

function mergeThemeOptions(
  options: unknown,
  chartType: unknown,
  isDark: boolean,
): ChartOptions {
  const theme = isDark ? DARK_THEME : LIGHT_THEME;
  const base = isRecord(options) ? options : {};
  const plugins = isRecord(base.plugins) ? base.plugins : {};
  const legend = isRecord(plugins.legend) ? plugins.legend : {};
  const legendLabels = isRecord(legend.labels) ? legend.labels : {};
  const title = isRecord(plugins.title) ? plugins.title : {};
  const scales = isRecord(base.scales) ? base.scales : {};

  return {
    responsive: true,
    maintainAspectRatio: false,
    color: theme.text,
    ...base,
    plugins: {
      ...plugins,
      legend: {
        ...legend,
        labels: {
          color: theme.text,
          ...legendLabels,
        },
      },
      title: {
        color: theme.title,
        ...title,
      },
    },
    scales: mergeScales(scales, chartType, theme),
  } as ChartOptions;
}

function mergeScales(
  scales: Record<string, unknown>,
  chartType: unknown,
  theme: typeof LIGHT_THEME,
): Record<string, unknown> {
  const names = new Set([...defaultScaleNames(chartType), ...Object.keys(scales)]);
  const merged: Record<string, unknown> = {};

  for (const name of names) {
    const scale = isRecord(scales[name]) ? scales[name] : {};
    const ticks = isRecord(scale.ticks) ? scale.ticks : {};
    const grid = isRecord(scale.grid) ? scale.grid : {};
    const title = isRecord(scale.title) ? scale.title : {};
    merged[name] = {
      ...scale,
      ticks: {
        color: theme.text,
        ...ticks,
      },
      grid: {
        color: theme.grid,
        ...grid,
      },
      border: {
        color: theme.border,
        ...(isRecord(scale.border) ? scale.border : {}),
      },
      title: {
        color: theme.title,
        ...title,
      },
    };
  }

  return merged;
}

function defaultScaleNames(chartType: unknown): string[] {
  if (chartType === "bar" || chartType === "line" || chartType === "scatter") {
    return ["x", "y"];
  }
  if (chartType === "bubble") {
    return ["x", "y"];
  }
  if (chartType === "radar" || chartType === "polarArea") {
    return ["r"];
  }
  return [];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Chart artifact renderer — embeds a Vega-Lite spec via react-vega.
// Respects the app's dark/light theme and resizes with the thread column.

import { useEffect, useMemo, useRef, useState } from "react";
import { VegaEmbed } from "react-vega";
import type { VisualizationSpec } from "vega-embed";
import { useTheme } from "next-themes";
import type { ChartArtifactSpec } from "../../../types";

// Vega-Lite JSON schema identifier.  Injected when the LLM-emitted spec
// is missing one so ``VegaEmbed`` picks the Vega-Lite grammar instead
// of falling back to base Vega.
const VEGA_LITE_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json";

// Fallback height when the spec doesn't supply one.  Width is measured
// from the container; height only needs a sensible default because
// Vega-Lite's internal default (200) is often too short for line charts.
const DEFAULT_CHART_HEIGHT = 320;

// Minimum width we'll bother rendering at — guards against the rare
// layout flash where ResizeObserver reports 0 before the parent lays out.
const MIN_CHART_WIDTH = 120;

// Minimal theme overrides so charts sit on dark and light backgrounds
// without manual colour tuning in every generated spec.  Vega-Lite's
// ``config`` is merged with the user's spec — the spec always wins on
// an explicit collision.
const LIGHT_CONFIG = {
  background: "transparent",
  axis: {
    labelColor: "#444",
    titleColor: "#111",
    gridColor: "#e5e7eb",
    domainColor: "#d1d5db",
    tickColor: "#d1d5db",
  },
  view: { stroke: "transparent" },
  legend: { labelColor: "#444", titleColor: "#111" },
};

const DARK_CONFIG = {
  background: "transparent",
  axis: {
    labelColor: "#cbd5e1",
    titleColor: "#f1f5f9",
    gridColor: "#374151",
    domainColor: "#4b5563",
    tickColor: "#4b5563",
  },
  view: { stroke: "transparent" },
  legend: { labelColor: "#cbd5e1", titleColor: "#f1f5f9" },
};

export function ArtifactChart({ spec }: { spec: ChartArtifactSpec }) {
  const { resolvedTheme } = useTheme();
  const isDark = resolvedTheme === "dark";

  // Measure the container so Vega gets an explicit numeric width.  The
  // `width: "container"` mode depends on the parent having a layout
  // width before vega-embed measures it; inside `flex`/`min-w-0`/
  // `overflow-auto` chains it reports zero and the chart renders into
  // a 0-wide box.  Passing a measured number sidesteps the problem.
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? el.offsetWidth;
      if (w >= MIN_CHART_WIDTH) setWidth(Math.floor(w));
    });
    observer.observe(el);
    // Seed with the first measurement synchronously.
    if (el.offsetWidth >= MIN_CHART_WIDTH) setWidth(el.offsetWidth);
    return () => observer.disconnect();
  }, []);

  const [error, setError] = useState<string | null>(null);

  const merged = useMemo<VisualizationSpec | null>(() => {
    if (width == null) return null;
    const base = (spec.vega_lite ?? {}) as Record<string, unknown>;
    const themeConfig = isDark ? DARK_CONFIG : LIGHT_CONFIG;
    return {
      $schema: VEGA_LITE_SCHEMA,
      width,
      height: DEFAULT_CHART_HEIGHT,
      ...base,
      config: {
        ...themeConfig,
        ...((base.config as Record<string, unknown>) ?? {}),
      },
    } as VisualizationSpec;
  }, [spec.vega_lite, isDark, width]);

  // Clear any prior error when the spec changes.
  useEffect(() => {
    setError(null);
  }, [spec.vega_lite]);

  return (
    <div className="flex flex-col gap-2">
      <div ref={containerRef} className="w-full">
        {merged && (
          <VegaEmbed
            spec={merged}
            options={{ actions: false, renderer: "svg" }}
            onError={(e) => setError(e instanceof Error ? e.message : String(e))}
          />
        )}
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

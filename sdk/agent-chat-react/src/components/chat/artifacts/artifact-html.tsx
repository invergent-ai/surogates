// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// HTML artifact renderer — sandboxed iframe with `srcdoc`.
//
// Security model: the iframe runs with `sandbox="allow-scripts"` and NO
// `allow-same-origin`, giving the document a unique opaque origin.
// Scripts run but cannot reach the parent frame, cookies, localStorage,
// or any same-origin API.  Forms and top-level navigation are also
// disallowed.  The iframe sandbox is the load-bearing security
// boundary; we intentionally do not sanitise the HTML payload because
// the whole point of this artifact kind is that it renders as-is.

import { useMemo, useState } from "react";
import { cn } from "../../../lib/utils";
import type { HtmlArtifactSpec } from "../../../types";

const DEFAULT_HEIGHT_PX = 480;
const EXPANDED_HEIGHT_PX = 800;

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2;
const ZOOM_STEP = 0.1;

/**
 * Defensive CSS preamble injected into every artifact's srcdoc.
 *
 * Models occasionally generate HTML that puts a Chart.js ``<canvas>``
 * inside a flex/auto-sized parent with ``maintainAspectRatio:false`` --
 * the classic footgun: chart.js sizes the canvas to fill its parent,
 * the parent (lacking a fixed height) sizes to fit the canvas, the
 * ResizeObserver fires, repeat forever.  The iframe stays at its own
 * fixed CSS height but the content inside it grows tall and the user
 * sees a runaway scrollbar inside the artifact.
 *
 * We cap every ``<canvas>`` at the iframe's viewport (80vh) so the
 * resize feedback loop terminates at a sensible ceiling.  The cap is
 * intentionally high enough to leave a legitimate full-iframe chart
 * looking right, and low enough to terminate the runaway.
 */
const SAFETY_STYLE_PREAMBLE = (
  "<style>html,body{max-height:100vh}" +
  "canvas{max-height:80vh!important}</style>"
);

function injectSafetyStyle(html: string): string {
  // Insert just before </head> so the rules cascade after the
  // document's own styles.  No </head> (or no <head> at all) means
  // the model emitted a body-only fragment -- prepend at the top so
  // the style still applies via the implicit <head> the browser
  // synthesises.
  const headCloseIdx = html.search(/<\/head\s*>/i);
  if (headCloseIdx >= 0) {
    return (
      html.slice(0, headCloseIdx) +
      SAFETY_STYLE_PREAMBLE +
      html.slice(headCloseIdx)
    );
  }
  return SAFETY_STYLE_PREAMBLE + html;
}

export function ArtifactHtml({
  spec,
  fill = false,
}: {
  spec: HtmlArtifactSpec;
  fill?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [zoom, setZoom] = useState(1);

  // Inject the safety preamble exactly once per spec.html so we don't
  // re-string-build on every zoom/expand state change.
  const safeSrcdoc = useMemo(() => injectSafetyStyle(spec.html), [spec.html]);

  const viewportHeight = expanded ? EXPANDED_HEIGHT_PX : DEFAULT_HEIGHT_PX;
  const clamp = (v: number) =>
    Math.round(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v)) * 100) / 100;

  return (
    <div
      className={cn(
        "flex flex-col gap-2",
        fill && "h-full min-h-0",
      )}
    >
      <div
        className={cn("w-full overflow-hidden", fill && "min-h-0 flex-1")}
        style={fill ? undefined : { height: `${viewportHeight}px` }}
      >
        <iframe
          title="artifact-html"
          srcDoc={safeSrcdoc}
          sandbox="allow-scripts"
          className={cn("border-0", fill && "h-full w-full")}
          style={
            fill
              ? { zoom }
              : {
                  // Keep width at 100% so the HTML's responsive CSS doesn't
                  // reflow as the user zooms.  Browsers resolve percentage
                  // widths against the parent after zoom, so this stays full
                  // width regardless of zoom.  Pixel heights *are* scaled by
                  // zoom, so compensate: at zoom 0.5 we set 960px which
                  // renders as 480px (no empty space below), and the inner
                  // viewport gets 960px of vertical room to show more
                  // content.  At zoom 1.5 the inner viewport shrinks to
                  // 320px and the iframe's own scrollbar handles overflow.
                  width: "100%",
                  height: `${viewportHeight / zoom}px`,
                  zoom,
                }
          }
        />
      </div>
      <div className="flex items-center justify-between">
        {spec.caption ? <span>{spec.caption}</span> : <span />}
        <div className="flex items-center gap-2 ">
          <button
            type="button"
            onClick={() => setZoom((z) => clamp(z - ZOOM_STEP))}
            disabled={zoom <= MIN_ZOOM}
            className="hover:text-foreground disabled:opacity-40 cursor-pointer"
            aria-label="zoom out"
          >
            −
          </button>
          <button
            type="button"
            onClick={() => setZoom(1)}
            className="tabular-nums hover:text-foreground"
            aria-label="reset zoom"
          >
            {Math.round(zoom * 100)}%
          </button>
          <button
            type="button"
            onClick={() => setZoom((z) => clamp(z + ZOOM_STEP))}
            disabled={zoom >= MAX_ZOOM}
            className="hover:text-foreground disabled:opacity-40"
            aria-label="zoom in"
          >
            +
          </button>
          {!fill && (
            <>
              <span className="text-border">|</span>
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                className="hover:text-foreground"
              >
                {expanded ? "collapse" : "expand"}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

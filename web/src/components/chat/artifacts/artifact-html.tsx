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

import { useState } from "react";
import { cn } from "@/lib/utils";
import type { HtmlArtifactSpec } from "@/types/session";

const DEFAULT_HEIGHT_PX = 480;
const EXPANDED_HEIGHT_PX = 800;

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2;
const ZOOM_STEP = 0.1;

export function ArtifactHtml({ spec }: { spec: HtmlArtifactSpec }) {
  const [expanded, setExpanded] = useState(false);
  const [zoom, setZoom] = useState(1);

  const viewportHeight = expanded ? EXPANDED_HEIGHT_PX : DEFAULT_HEIGHT_PX;
  const clamp = (v: number) =>
    Math.round(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v)) * 100) / 100;

  return (
    <div className="flex flex-col gap-2">
      <div
        className="w-full overflow-hidden rounded-md border border-border bg-white"
        style={{ height: `${viewportHeight}px` }}
      >
        <iframe
          title="artifact-html"
          srcDoc={spec.html}
          sandbox="allow-scripts"
          className={cn("border-0 bg-white")}
          style={{
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
          }}
        />
      </div>
      <div className="flex items-center justify-between">
        {spec.caption ? <span>{spec.caption}</span> : <span />}
        <div className="flex items-center gap-2 font-mono">
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
          <span className="text-border">|</span>
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="hover:text-foreground"
          >
            {expanded ? "collapse" : "expand"}
          </button>
        </div>
      </div>
    </div>
  );
}

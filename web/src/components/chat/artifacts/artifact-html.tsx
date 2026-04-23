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

// Default height for the preview.  Users can toggle to a larger size
// for content that's taller than the default.
const DEFAULT_HEIGHT_PX = 480;
const EXPANDED_HEIGHT_PX = 800;

export function ArtifactHtml({ spec }: { spec: HtmlArtifactSpec }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="flex flex-col gap-2">
      <iframe
        title="artifact-html"
        srcDoc={spec.html}
        sandbox="allow-scripts"
        className={cn(
          "w-full rounded-md border border-border bg-white transition-[height]",
        )}
        style={{
          height: `${expanded ? EXPANDED_HEIGHT_PX : DEFAULT_HEIGHT_PX}px`,
        }}
      />
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        {spec.caption ? <span>{spec.caption}</span> : <span />}
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="font-mono text-xs text-muted-foreground hover:text-foreground"
        >
          {expanded ? "collapse" : "expand"}
        </button>
      </div>
    </div>
  );
}

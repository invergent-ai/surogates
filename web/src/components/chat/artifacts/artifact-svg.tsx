// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// SVG artifact renderer.
//
// Security model: the SVG is served via `<img src="data:image/svg+xml,...">`.
// Browsers explicitly disable script execution when an SVG is loaded
// through an <img> element, which is what makes this safe without a
// separate sanitisation step.  (The same SVG inlined via innerHTML
// WOULD run any <script> tags it contained — so we never do that.)

import { useMemo } from "react";
import type { SvgArtifactSpec } from "@/types/session";

export function ArtifactSvg({ spec }: { spec: SvgArtifactSpec }) {
  const dataUrl = useMemo(() => {
    // URL-encode rather than base64 so the payload stays readable in
    // devtools and avoids the ~33% base64 size tax for large SVGs.
    return `data:image/svg+xml;utf8,${encodeURIComponent(spec.svg)}`;
  }, [spec.svg]);

  return (
    <div className="flex flex-col items-center gap-2">
      <img
        src={dataUrl}
        alt={spec.caption ?? "SVG artifact"}
        className="max-h-[600px] w-full object-contain"
      />
      {spec.caption && (
        <p className="text-xs text-muted-foreground">{spec.caption}</p>
      )}
    </div>
  );
}

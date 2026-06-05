// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Markdown artifact renderer — reuses the chat's Streamdown pipeline
// so code blocks, math, mermaid, etc. work identically to assistant
// messages.

import { MessageResponse } from "../../ai-elements/message";
import { ScrollArea } from "../../ui/scroll-area";
import type { MarkdownArtifactSpec } from "../../../types";

export function ArtifactMarkdown({
  spec,
  fill = false,
}: {
  spec: MarkdownArtifactSpec;
  /**
   * When true (full-screen dialog) the content grows to fill the
   * available vertical space and the parent handles scroll.
   * When false (inline card) the body is capped at a fixed height
   * and scrolls internally via the shared ``ScrollArea`` styling so
   * a long report cannot dominate the chat thread.
   */
  fill?: boolean;
}) {
  const body = (
    <div className="px-4 py-3">
      <MessageResponse>{spec.content ?? ""}</MessageResponse>
    </div>
  );

  if (fill) return body;

  // Inline-mode cap: ~28rem keeps the artifact visible without
  // pushing the rest of the conversation off-screen.  The
  // ``Maximize2`` toolbar button on ArtifactCard opens the full
  // dialog where ``fill=true`` lets the body grow to 95vh.
  //
  // Radix's ScrollArea relies on a defined height on its Root for
  // the Viewport's ``height: 100%`` to resolve -- a bare ``max-h``
  // resolves to ``auto`` and the Viewport's content escapes the
  // card visually (you can see assistant prose underneath bleeding
  // through it).  Wrap in a fixed-height flex column with
  // ``flex-1 min-h-0`` on the ScrollArea so the height contract is
  // a real pixel value, not a hopeful percentage.
  return (
    <div className="flex h-112 flex-col">
      <ScrollArea className="flex-1 min-h-0">{body}</ScrollArea>
    </div>
  );
}

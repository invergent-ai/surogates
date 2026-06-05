// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Markdown artifact renderer — reuses the chat's Streamdown pipeline
// so code blocks, math, mermaid, etc. work identically to assistant
// messages.

import { MessageResponse } from "../../ai-elements/message";
import { cn } from "../../../lib/utils";
import type { MarkdownArtifactSpec } from "../../../types";

export function ArtifactMarkdown({
  spec,
  fill = false,
}: {
  spec: MarkdownArtifactSpec;
  /**
   * When true (full-screen dialog) the content grows to fill the
   * available vertical space and the parent handles scroll.
   * When false (inline card) the body is capped at ~28rem and
   * scrolls internally so a long report cannot dominate the chat
   * thread.
   */
  fill?: boolean;
}) {
  // Radix's ScrollArea expects a defined-height ancestor for the
  // Viewport's ``height: 100%`` to resolve; with ``max-h`` alone the
  // content visibly escapes the card and overlaps the assistant
  // prose underneath.  A plain div with ``max-h`` + ``overflow-y-auto``
  // sidesteps that entirely: the scrolling happens on the SAME
  // element that carries the height cap, so there's no percentage
  // resolution to fight with.  The browser's native scrollbar isn't
  // as styled as the Radix one but it's correct, and short
  // artifacts still collapse to their natural height (no forced
  // 28rem dead space).
  return (
    <div
      className={cn(
        "px-4 py-3",
        !fill && "max-h-112 overflow-y-auto",
      )}
    >
      <MessageResponse>{spec.content ?? ""}</MessageResponse>
    </div>
  );
}

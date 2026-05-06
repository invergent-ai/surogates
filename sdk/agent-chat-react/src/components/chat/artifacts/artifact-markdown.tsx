// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Markdown artifact renderer — reuses the chat's Streamdown pipeline
// so code blocks, math, mermaid, etc. work identically to assistant
// messages.

import { MessageResponse } from "../../ai-elements/message";
import type { MarkdownArtifactSpec } from "../../../types";

export function ArtifactMarkdown({ spec }: { spec: MarkdownArtifactSpec }) {
  return <MessageResponse>{spec.content ?? ""}</MessageResponse>;
}

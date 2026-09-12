// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import type { AgentChatMessage } from "../types";

export function formatReasoningTokens(tokens: number): string {
  if (tokens < 1000) return String(tokens);
  return `${(tokens / 1000).toFixed(1).replace(/\.0$/, "")}k`;
}

export function reasoningTokenLabel(message: AgentChatMessage, live: boolean): string {
  // Count arriving reasoning deltas while streaming. Provider usage
  // takes precedence once it arrives, including during history replay.
  const tokens = message.reasoningTokens ?? message.reasoningDeltaCount;
  if (tokens === undefined) return live ? "Thinking..." : "Thought";
  return `${live ? "Thinking..." : "Thought ·"} ${formatReasoningTokens(tokens)} tokens`;
}

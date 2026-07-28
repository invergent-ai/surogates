// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The buyer-facing usage unit. Commerce balances arrive from the
// harness token-denominated (period_token_remaining and friends), but
// end-users reason in "messages": one question plus the agent's
// answer. Mirrors surogate-ops frontend/src/utils/usage-units.ts so
// the buy page and this app describe the same purchase identically.

/**
 * How many tokens one message stands for. Purely presentational: the
 * API payloads stay token-denominated.
 */
export const TOKENS_PER_MESSAGE = 1_000;

/** Display-side conversion, floored: a balance shows what the holder
 * can still do, so 1,999 tokens reads as 1 message, not 2. */
export function tokensToMessages(tokens: number): number {
  if (!Number.isFinite(tokens) || tokens <= 0) {
    return 0;
  }
  return Math.floor(tokens / TOKENS_PER_MESSAGE);
}

export function formatMessageCount(messages: number): string {
  return messages.toLocaleString("en-US");
}

/**
 * Human label for a token amount: "~1,500 messages", "~1 message",
 * "under 1 message" (a positive balance too small to floor to one),
 * or "0 messages". The tilde is deliberate: a message is an average,
 * long conversations use more.
 */
export function approxMessagesLabel(tokens: number): string {
  if (!Number.isFinite(tokens) || tokens <= 0) {
    return "0 messages";
  }
  const messages = tokensToMessages(tokens);
  if (messages < 1) {
    return "under 1 message";
  }
  return `~${formatMessageCount(messages)} ${
    messages === 1 ? "message" : "messages"
  }`;
}

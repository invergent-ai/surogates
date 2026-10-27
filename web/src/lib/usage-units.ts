// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The buyer-facing usage unit. Commerce balances arrive from the
// harness token-denominated (period_token_remaining and friends), and a
// turn (one question and the agent's whole answer) is charged every
// model call it makes, tool steps included. Ops measures what a turn of
// this agent costs (``tokens_per_turn_estimate`` on the commerce
// overview); amounts render as turns with it and as plain tokens
// without. Mirrors surogate-ops frontend/src/utils/usage-units.ts so the
// buy page and this app describe the same purchase identically.

const COMPACT = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});

/** "300K tokens", "2.5M tokens", "0 tokens". */
function formatTokens(tokens: number): string {
  const n = Number.isFinite(tokens) && tokens > 0 ? tokens : 0;
  return `${COMPACT.format(n)} tokens`;
}

function usable(perTurn: number | null | undefined): perTurn is number {
  return perTurn != null && Number.isFinite(perTurn) && perTurn > 0;
}

/** Whole turns a balance pays for, floored: 1.9 turns is 1 turn. */
function tokensToTurns(tokens: number, perTurn: number): number {
  if (!Number.isFinite(tokens) || tokens <= 0 || !usable(perTurn)) {
    return 0;
  }
  return Math.floor(tokens / perTurn);
}

/**
 * "~3 turns", "~1 turn", "under 1 turn", "0 turns" for a token amount
 * at the agent's measured cost per turn; the plain token amount when
 * no estimate is known. The tilde is deliberate: turns vary, a long
 * tool-using answer costs more than a short reply.
 */
export function approxTurnsLabel(
  tokens: number,
  perTurn: number | null | undefined,
): string {
  if (!usable(perTurn)) {
    return formatTokens(tokens);
  }
  if (!Number.isFinite(tokens) || tokens <= 0) {
    return "0 turns";
  }
  const turns = tokensToTurns(tokens, perTurn);
  if (turns < 1) {
    return "under 1 turn";
  }
  return `~${turns.toLocaleString("en-US")} ${turns === 1 ? "turn" : "turns"}`;
}

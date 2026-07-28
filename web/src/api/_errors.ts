// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Shared error unwrapping for REST responses: tries to lift ``detail``
// from the JSON body, falling back to a caller-supplied message.

/**
 * FastAPI error bodies carry either a plain string or a structured object
 * in ``detail`` (e.g. the 402 insufficient-credits payload with ``error``,
 * ``resource`` and ``hint`` fields). Flatten both shapes to a human-readable
 * message so callers never render "[object Object]". Returns ``undefined``
 * when the body carried nothing usable so callers fall back to their own
 * static message.
 */
export function errorDetailMessage(detail: unknown): string | undefined {
  if (typeof detail === "string") {
    return detail || undefined;
  }
  if (detail && typeof detail === "object") {
    return objectDetailMessage(detail as Record<string, unknown>);
  }
  return undefined;
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

function objectDetailMessage(
  detail: Record<string, unknown>,
): string | undefined {
  const errorCode = nonEmptyString(detail.error)?.replaceAll("_", " ");
  const headline = nonEmptyString(detail.message) ?? errorCode;
  const hint = nonEmptyString(detail.hint);
  const parts = [headline, hint].filter((p): p is string => p !== undefined);
  if (parts.length > 0) {
    return parts.join(" — ");
  }
  try {
    return JSON.stringify(detail);
  } catch {
    return undefined;
  }
}

export async function parseError(
  response: Response,
  fallback: string,
): Promise<never> {
  const payload = (await response.json().catch(() => null)) as {
    detail?: unknown;
  } | null;
  throw new Error(errorDetailMessage(payload?.detail) ?? fallback);
}

// Lead copy per 402 paywall code emitted by the commerce/allowance gates
// (``{code, buy_url}``). ``operator_subscription_exhausted`` is the agent
// owner's problem, not the visitor's, so it offers no buy action.
const PAYWALL_LEADS: Record<string, string> = {
  allowance_exhausted: "You've reached your usage limit for this assistant.",
  subscription_required:
    "A subscription is required to keep chatting with this assistant.",
  operator_subscription_exhausted:
    "This assistant is temporarily unavailable. Its owner has run out of credit.",
  sign_in_required: "Please sign in to keep chatting with this assistant.",
};

/**
 * Turn a 402 paywall detail (``{code, buy_url}``) into a user-facing
 * message, appending the agent's buy link when there is a buyer action to
 * take. Returns ``undefined`` when the detail is not a recognised paywall
 * payload so callers fall back to their generic handling.
 */
export function paywallErrorMessage(detail: unknown): string | undefined {
  if (!detail || typeof detail !== "object") {
    return undefined;
  }
  const record = detail as Record<string, unknown>;
  const code = nonEmptyString(record.code);
  if (!code) {
    return undefined;
  }
  const lead =
    PAYWALL_LEADS[code] ?? "You need to buy access to keep chatting.";
  const buyUrl = nonEmptyString(record.buy_url);
  if (buyUrl && code !== "operator_subscription_exhausted") {
    return `${lead} Get more access here: ${buyUrl}`;
  }
  return lead;
}

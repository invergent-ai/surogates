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

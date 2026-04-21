// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Shared error unwrapping for REST responses: tries to lift ``detail``
// from the JSON body, falling back to a caller-supplied message.

export async function parseError(
  response: Response,
  fallback: string,
): Promise<never> {
  const payload = (await response.json().catch(() => null)) as {
    detail?: string;
  } | null;
  throw new Error(payload?.detail ?? fallback);
}

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Surogate Desktop's sign-in, as the web client serves it in the system browser at
// /oauth/authorize (surogates/api/routes/oauth.py): the request the desktop sent, and the
// user's answer to it. The agent checks every value; the page only carries them.

export interface ConsentRequest {
  response_type: string;
  client_id: string;
  redirect_uri: string;
  state: string;
  code_challenge: string;
  code_challenge_method: string;
  computer: string; // what the desktop calls this computer: shown, never trusted
}

const FIELDS = ["response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method"] as const;

/** The desktop's request in the page's query string; null when a field it always sends is missing. */
export function consentRequest(search: string): ConsentRequest | null {
  const query = new URLSearchParams(search);
  const fields: Record<string, string> = {};
  for (const field of FIELDS) {
    const value = query.get(field);
    if (!value) return null;
    fields[field] = value;
  }
  const computer = (query.get("computer") ?? "").trim().slice(0, 100) || "this computer";
  return { ...(fields as Omit<ConsentRequest, "computer">), computer };
}

export type ConsentOutcome =
  | { kind: "redirect"; to: string } // the browser goes back to the desktop
  | { kind: "sign-in-again" } // allowing needs a sign-in from the last few minutes
  | { kind: "refused"; message: string };

/** Send the user's *decision* to the agent through *post*: where the browser goes next, or why it goes nowhere. */
export async function decide(
  request: ConsentRequest,
  decision: "allow" | "deny",
  post: (body: Record<string, string>) => Promise<Response>,
): Promise<ConsentOutcome> {
  const { computer: _shown, ...asked } = request;
  const response = await post({ ...asked, decision });
  const body = (await response.json().catch(() => null)) as { redirect_to?: unknown; detail?: unknown } | null;
  if (response.ok && typeof body?.redirect_to === "string") return { kind: "redirect", to: body.redirect_to };
  const detail = body?.detail;
  const code = typeof detail === "object" && detail !== null ? (detail as { code?: unknown }).code : undefined;
  if (response.status === 401 || code === "recent_sign_in_required") return { kind: "sign-in-again" };
  return { kind: "refused", message: typeof detail === "string" ? detail : "This sign-in request did not come from Surogate Desktop." };
}

// The agent allows the desktop only on a sign-in from its last 10 minutes; a minute less here, for the clocks.
const RECENT_S = 9 * 60;

/**
 * Whether *accessToken* says its user signed in recently enough to allow the desktop. Read, not
 * checked, and only to ask for a new sign-in before the user clicks Allow: the agent decides.
 */
export function recentSignIn(accessToken: string | null, now: number = Date.now()): boolean {
  const payload = accessToken?.split(".")[1];
  if (!payload) return false;
  try {
    const { auth_time: authTime } = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/"))) as { auth_time?: unknown };
    return typeof authTime === "number" && now / 1000 - authTime < RECENT_S;
  } catch {
    return false;
  }
}

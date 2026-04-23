// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Client for the clarify-response endpoint.  Submits the user's batched
// answers to an active `clarify` tool call.  The server emits a
// CLARIFY_RESPONSE event which the worker's clarify handler polls for
// before returning to the LLM.

import { authFetch } from "./auth";
import type { ClarifyAnswer } from "@/types/session";

export interface ClarifyResponseReply {
  event_id: number;
}

export async function submitClarifyResponse(
  sessionId: string,
  toolCallId: string,
  responses: ClarifyAnswer[],
): Promise<ClarifyResponseReply> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/clarify/${encodeURIComponent(toolCallId)}/respond`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ responses }),
    },
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Failed to submit clarify response");
  }
  return (await response.json()) as ClarifyResponseReply;
}

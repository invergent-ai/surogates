// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Client for the turn-feedback endpoint. Lets the UI record a
// thumbs-up or thumbs-down on any rate-able assistant turn event
// (an expert.result, emitting EXPERT_ENDORSE/EXPERT_OVERRIDE, or an
// llm.response, emitting USER_FEEDBACK).  The backend routes by the
// target event's type, so this client is event-agnostic.

import { authFetch } from "./auth";
import { errorDetailMessage } from "./_errors";

export type FeedbackRating = "up" | "down";

export interface FeedbackResponse {
  event_id: number;
  event_type: string;
}

export async function submitTurnFeedback(
  sessionId: string,
  targetEventId: number,
  rating: FeedbackRating,
  reason?: string,
): Promise<FeedbackResponse> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/events/${targetEventId}/feedback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating, reason }),
    },
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Failed to submit feedback");
  }
  return (await response.json()) as FeedbackResponse;
}

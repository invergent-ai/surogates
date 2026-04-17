// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Client for the expert-feedback endpoint. Lets the UI record a
// thumbs-up (EXPERT_ENDORSE) or thumbs-down (EXPERT_OVERRIDE) on a
// specific expert.result event.

import { authFetch } from "./auth";

export type FeedbackRating = "up" | "down";

export interface FeedbackResponse {
  event_id: number;
  event_type: string;
}

export async function submitExpertFeedback(
  sessionId: string,
  expertResultEventId: number,
  rating: FeedbackRating,
  reason?: string,
): Promise<FeedbackResponse> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/events/${expertResultEventId}/feedback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rating, reason }),
    },
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Failed to submit feedback");
  }
  return (await response.json()) as FeedbackResponse;
}

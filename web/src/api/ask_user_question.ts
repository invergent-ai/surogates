// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Client for the ask_user_question-response endpoint.  Submits the user's
// batched answers to an active `ask_user_question` tool call.  The server
// emits an ASK_USER_QUESTION_RESPONSE event which the worker's
// ask_user_question handler polls for before returning to the LLM.

import { authFetch } from "./auth";
import type { AskUserQuestionAnswer } from "@/types/session";

export interface AskUserQuestionResponseReply {
  event_id: number;
}

export async function submitAskUserQuestionResponse(
  sessionId: string,
  toolCallId: string,
  responses: AskUserQuestionAnswer[],
): Promise<AskUserQuestionResponseReply> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/ask_user_question/${encodeURIComponent(toolCallId)}/respond`,
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
    throw new Error(err?.detail ?? "Failed to submit ask_user_question response");
  }
  return (await response.json()) as AskUserQuestionResponseReply;
}

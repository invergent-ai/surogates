// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Reading an ask_user_question payload out of an inbox item, and
// shaping a half-filled answer back into the response the tool expects.
// Separate from the panel so the rules stay testable on their own.
//
// Not exported from the package: the only other host with its own inbox
// UI is Studio, whose pin predates this file. Export it when something
// can actually import it.

import type {
  AgentChatAskUserQuestionAnswer,
  AgentChatAskUserQuestionChoice,
  AgentChatInboxItem,
} from "../../types";

export interface InboxQuestion {
  prompt: string;
  choices?: AgentChatAskUserQuestionChoice[];
  /** Mirrors the tool schema, where omitting it means "other allowed". */
  allowOther: boolean;
}

/** How far the user has got in answering one question. */
export interface AnswerDraft {
  /** Label chosen from the menu. */
  picked?: string;
  /** Free text: the whole answer, or the "Other" value. */
  typed?: string;
  /** The user moved off the menu to the free-text field. */
  useOther?: boolean;
}

export function hasChoices(question: InboxQuestion): boolean {
  return (question.choices?.length ?? 0) > 0;
}

export function parseInboxQuestions(item: AgentChatInboxItem): InboxQuestion[] {
  const raw = (item.payload as { questions?: unknown }).questions;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((entry) => {
      const question = entry as {
        prompt?: unknown;
        choices?: unknown;
        allow_other?: unknown;
      };
      const choices = Array.isArray(question.choices)
        ? (question.choices as Array<{ label?: unknown; description?: unknown }>)
            .map((choice) => ({
              label: typeof choice.label === "string" ? choice.label : "",
              description:
                typeof choice.description === "string"
                  ? choice.description
                  : undefined,
            }))
            .filter((choice) => choice.label)
        : undefined;
      return {
        prompt: typeof question.prompt === "string" ? question.prompt : "",
        choices,
        allowOther: question.allow_other !== false,
      };
    })
    .filter((question) => question.prompt);
}

/**
 * Whether the answer departed from the options the agent offered.
 *
 * Only a question that presented a menu can be answered off it; an
 * open-ended question has nothing to deviate from, so its answer is
 * never "other".
 *
 * The server settles this from the questions it stored and overrides
 * whatever is submitted, so this is what the UI shows and a fallback
 * for servers predating that; it is not the authority.
 */
export function isOtherAnswer(
  question: InboxQuestion,
  draft: AnswerDraft | undefined,
): boolean {
  return hasChoices(question) && !!draft?.useOther;
}

export function answerText(
  question: InboxQuestion,
  draft: AnswerDraft | undefined,
): string {
  const fromMenu = hasChoices(question) && !draft?.useOther;
  return (fromMenu ? (draft?.picked ?? "") : (draft?.typed ?? "")).trim();
}

export function buildInboxResponse(
  question: InboxQuestion,
  draft: AnswerDraft | undefined,
): AgentChatAskUserQuestionAnswer {
  return {
    question: question.prompt,
    answer: answerText(question, draft),
    is_other: isOtherAnswer(question, draft),
  };
}

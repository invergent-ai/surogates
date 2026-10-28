// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// What counts as an ask_user_question the user can see and answer.
// Plain data, no React: the chat thread, the question card, the orb and
// the inbox all read questions through here, so they can never disagree
// about whether a call is asking something.

import { effectiveStatus, parseArgs } from "./shared";
import type { AskUserQuestionQuestion, ToolCallInfo } from "../../../types";

/** Mirrors ``MAX_QUESTIONS`` in surogates/tools/builtin/ask_user_question.py. */
const MAX_QUESTIONS = 5;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonBlank(value: unknown): value is string {
  return typeof value === "string" && value.trim() !== "";
}

/**
 * The questions of a call, normalised, or ``null`` when the server's
 * ``_validate_questions`` would reject the call.
 *
 * All or nothing, like the server: one bad question fails the whole
 * call, so no partial ask is ever shown or answerable.  The choice
 * count is not checked: its cap depends on the channel, which the
 * client does not know, and the server's rejection still reaches us as
 * the call's result.
 */
export function validateAskQuestions(raw: unknown): AskUserQuestionQuestion[] | null {
  if (!Array.isArray(raw) || raw.length === 0 || raw.length > MAX_QUESTIONS) return null;
  const questions: AskUserQuestionQuestion[] = [];
  for (const q of raw) {
    if (!isObject(q) || !nonBlank(q.prompt)) return null;
    const question: AskUserQuestionQuestion = {
      prompt: q.prompt.trim(),
      allow_other: typeof q.allow_other === "boolean" ? q.allow_other : true,
    };
    if (q.choices != null) {
      if (!Array.isArray(q.choices)) return null;
      const choices = [];
      for (const c of q.choices) {
        if (!isObject(c) || !nonBlank(c.label)) return null;
        choices.push({
          label: c.label.trim(),
          ...(nonBlank(c.description) ? { description: c.description.trim() } : {}),
        });
      }
      if (choices.length > 0) question.choices = choices;
    }
    questions.push(question);
  }
  return questions;
}

/** The questions a call is presenting; empty when the call is not a valid ask. */
export function askQuestionsOf(tc: ToolCallInfo): AskUserQuestionQuestion[] {
  const args = parseArgs<{ questions?: unknown }>(tc.args);
  return validateAskQuestions(isObject(args) ? args.questions : undefined) ?? [];
}

/**
 * Whether the tool refused the call (its result is an ``error``).
 *
 * Only a completed call: a session that failed mid-question marks the
 * call ``error`` with ``[failed]``, and that question was asked.
 */
export function askRejected(tc: ToolCallInfo): boolean {
  // Recorded answers mean the question was shown; a later tool failure
  // must not hide it or them.
  if (tc.askUserQuestionAnswers !== undefined) return false;
  return tc.status === "complete" && effectiveStatus(tc) === "error";
}

/** Whether a call is a question the user can see: well-formed and not refused. */
export function isAsk(tc: ToolCallInfo): boolean {
  return (
    tc.toolName === "ask_user_question" &&
    !askRejected(tc) &&
    askQuestionsOf(tc).length > 0
  );
}

/** Whether a call is a question waiting on the user right now. */
export function isLiveAsk(tc: ToolCallInfo): boolean {
  return tc.status === "running" && isAsk(tc);
}

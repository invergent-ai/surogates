// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// ask_user_question renders in one of two shapes, chosen by how many
// questions the agent asked.
//
// A SINGLE question is a conversational turn, not a form: the agent
// said something and is waiting for a reply.  It renders as the agent's
// own message with optional quick-reply chips, the composer stays live,
// and the answer lands in the thread as the user's own message (see
// ``appendConversationalAnswer`` in the reducer).  Wrapping that in a
// bordered card with tab headers, per-question numbering and a Submit
// button turned every conversational beat into a form receipt.
//
// TWO OR MORE questions is a genuine batch decision: tabs for each
// question, radio choices with labels + descriptions, an optional
// "Other" free-form row, and a single Submit that batches every answer
// back to the worker.  Picking a choice auto-advances to the next
// unanswered question ("Other" waits for typed input; Enter advances
// it) so multi-question flows read select -> next -> submit.  Esc
// pauses the session (= user chose to stop the chat instead of
// answering).

import { useCallback, useEffect, useMemo, useState } from "react";
import { XIcon } from "lucide-react";
import { cn } from "../../../lib/utils";
import { Input } from "../../ui/input";
import { MessageResponse } from "../../ai-elements/message";
import { useAgentChatAdapterContext } from "../../../adapter-context";
import { parseArgs } from "./shared";
import type { ToolCallInfo } from "../../../types";
import type {
  AskUserQuestionAnswer,
  AskUserQuestionArgs,
  AskUserQuestionChoice,
  AskUserQuestionQuestion,
} from "../../../types";

// Sentinel choice index for the "Other" option.  Indexes into `choices`
// are 0..(N-1); OTHER_INDEX is N so pickers can round-trip cleanly.
const OTHER_INDEX_OFFSET = 1_000_000;

type Selection = {
  // Index into question.choices, or a value >= OTHER_INDEX_OFFSET when
  // the user picked the "Other" row.  -1 = no selection yet.
  index: number;
  other: string;
};

function emptySelection(): Selection {
  return { index: -1, other: "" };
}

function buildAnswer(q: AskUserQuestionQuestion, sel: Selection): AskUserQuestionAnswer | null {
  if (sel.index < 0) return null;
  if (sel.index >= OTHER_INDEX_OFFSET) {
    const text = sel.other.trim();
    if (!text) return null;
    // "Other" only means something when there was a menu to depart
    // from; mirrors the server-side rule in resolve_text_answer.
    return { question: q.prompt, answer: text, is_other: (q.choices?.length ?? 0) > 0 };
  }
  const choice = q.choices?.[sel.index];
  if (!choice) return null;
  return { question: q.prompt, answer: choice.label, is_other: false };
}

// ── Shape helpers (shared with chat-thread) ──────────────────────────

/** The questions an ask_user_question call is presenting, if parseable. */
export function askQuestionsOf(tc: ToolCallInfo): AskUserQuestionQuestion[] {
  return parseArgs<AskUserQuestionArgs>(tc.args)?.questions ?? [];
}

/**
 * A one-question ask is conversational: the agent is talking, not
 * collecting a form.  Everything else keeps the batch widget.
 */
export function isConversationalAsk(questions: AskUserQuestionQuestion[]): boolean {
  return questions.length === 1;
}

/**
 * Whether a pending conversational ask accepts a typed reply.
 *
 * The server converts a free-text message into the answer for whatever
 * question is pending, so the composer is the natural way to reply —
 * except when the agent offered a closed menu (``allow_other: false``
 * alongside choices), where only the listed options are valid.  A
 * question with no choices always accepts typing; there would
 * otherwise be no way to answer it at all.
 */
export function conversationalAskAcceptsFreeText(
  question: AskUserQuestionQuestion,
): boolean {
  const hasChoices = (question.choices?.length ?? 0) > 0;
  return !hasChoices || question.allow_other !== false;
}

/**
 * Whether the agent's message body already contains the question, so
 * rendering the prompt again would echo it.
 *
 * Agents routinely write the question in prose and pass the same
 * sentence as ``prompt``.  Comparison is whitespace- and case-
 * insensitive and ignores inline markdown emphasis, since the body is
 * markdown and the prompt is plain text.
 *
 * The body normally *ends* with the question, after a lead-in, so that
 * is the primary test.  A mid-body match also counts, but only for a
 * prompt long enough to be unmistakable: a short one ("Sure?") occurs
 * inside ordinary prose by coincidence ("I'm not sure? Let me...") and
 * suppressing on that would leave the user with chips, or nothing at
 * all, and no visible question to answer.
 */
const UNMISTAKABLE_PROMPT_LENGTH = 40;

export function promptEchoedInContent(
  content: string | undefined,
  prompt: string,
): boolean {
  const normalize = (s: string) =>
    s.replace(/[*_`]/g, "").replace(/\s+/g, " ").trim().toLowerCase();
  const body = normalize(content ?? "");
  const question = normalize(prompt);
  if (!body || !question) return false;
  if (body.endsWith(question)) return true;
  return (
    question.length >= UNMISTAKABLE_PROMPT_LENGTH && body.includes(question)
  );
}

// ── Entry point ──────────────────────────────────────────────────────

export function AskUserQuestionToolBlock({
  tc,
  assistantContent,
  viewMode = "simple",
}: {
  tc: ToolCallInfo;
  /**
   * The body of the assistant message that made this call, used to
   * suppress a prompt the agent already wrote out in prose.
   */
  assistantContent?: string;
  /**
   * Passed down rather than read from the persisted view-mode store:
   * the two render paths are mode-exclusive (IterationGroup is Simple,
   * the timeline is Expert), and a host that drives ChatThread with an
   * explicit ``viewMode`` prop must not see the widget disagree with
   * the thread around it.
   */
  viewMode?: "simple" | "expert";
}) {
  const questions = useMemo(() => askQuestionsOf(tc), [tc.args]);

  if (questions.length === 0) {
    return (
      <div className="rounded-md border border-destructive/50 bg-destructive/5 px-3 py-2 text-xs text-destructive">
        ask_user_question: no questions provided
      </div>
    );
  }

  if (isConversationalAsk(questions)) {
    return (
      <ConversationalAsk
        tc={tc}
        question={questions[0]!}
        assistantContent={assistantContent}
      />
    );
  }

  return <BatchAsk tc={tc} questions={questions} viewMode={viewMode} />;
}

// ── Conversational (single question) ─────────────────────────────────

function ConversationalAsk({
  tc,
  question,
  assistantContent,
}: {
  tc: ToolCallInfo;
  question: AskUserQuestionQuestion;
  assistantContent?: string;
}) {
  const { adapter, sessionId } = useAgentChatAdapterContext();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const answered = tc.askUserQuestionAnswers !== undefined;
  const pending = !answered && tc.status === "running";
  // No answer and the call is over: the wait timed out, or the session
  // was paused/ended while the question was open.
  const unanswered = !answered && tc.status !== "running";

  const choices = question.choices ?? [];
  const showPrompt = !promptEchoedInContent(assistantContent, question.prompt);

  // Declining to answer is a supported outcome -- the tool documents
  // that a paused session comes back as ``cancelled: true``. The batch
  // widget binds it to Esc; the conversational shape has no visible
  // chrome to hang it on, but must not drop the capability, since the
  // composer deliberately shows Send rather than Stop here.
  useEffect(() => {
    if (!pending || !sessionId) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      void adapter.pauseSession({ sessionId }).catch(() => {
        // Best-effort; the user may press Esc again.
      });
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [adapter, sessionId, pending]);

  const pick = useCallback(
    async (label: string) => {
      if (!sessionId || submitting) return;
      setError(null);
      setSubmitting(true);
      try {
        await adapter.submitAskUserQuestionResponse({
          sessionId,
          toolCallId: tc.id,
          responses: [
            { question: question.prompt, answer: label, is_other: false },
          ],
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Submit failed.");
        setSubmitting(false);
      }
    },
    [adapter, sessionId, tc.id, question.prompt, submitting],
  );

  // Descriptions need room to read, so they get stacked rows; bare
  // labels stay compact as inline pills.
  const stacked = choices.some((c) => !!c.description);

  return (
    <div className="space-y-2">
      {showPrompt && <MessageResponse>{question.prompt}</MessageResponse>}

      {pending && choices.length > 0 && (
        <div
          className={cn(
            stacked ? "flex flex-col gap-1.5" : "flex flex-wrap gap-2",
            "max-w-2xl",
          )}
          role="group"
          aria-label="Suggested answers"
        >
          {choices.map((choice, i) => (
            <QuickReply
              key={i}
              choice={choice}
              stacked={stacked}
              disabled={submitting}
              onSelect={() => void pick(choice.label)}
            />
          ))}
        </div>
      )}

      {error && <p className="text-xs text-destructive">{error}</p>}

      {unanswered && (
        <p className="text-xs italic text-muted-foreground/70">
          No answer recorded.
        </p>
      )}
    </div>
  );
}

function QuickReply({
  choice,
  stacked,
  disabled,
  onSelect,
}: {
  choice: AskUserQuestionChoice;
  stacked: boolean;
  disabled: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onSelect}
      className={cn(
        "border border-border bg-background text-left text-foreground transition-colors",
        "hover:border-foreground/30 hover:bg-muted",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
        stacked
          ? "rounded-md px-3 py-2"
          : "rounded-full px-3.5 py-1.5 text-sm",
      )}
    >
      <span className={cn("block", stacked && "text-sm")}>{choice.label}</span>
      {stacked && choice.description && (
        <span className="mt-0.5 block text-xs text-muted-foreground">
          {choice.description}
        </span>
      )}
    </button>
  );
}

// ── Batch (two or more questions) ────────────────────────────────────

function BatchAsk({
  tc,
  questions,
  viewMode,
}: {
  tc: ToolCallInfo;
  questions: AskUserQuestionQuestion[];
  viewMode: "simple" | "expert";
}) {
  const { adapter, sessionId } = useAgentChatAdapterContext();

  const [active, setActive] = useState(0);
  const [selections, setSelections] = useState<Selection[]>(() =>
    questions.map(emptySelection),
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const locked = tc.askUserQuestionAnswers !== undefined || tc.status !== "running";

  // Clamp active tab when questions change (shouldn't, but defensive).
  useEffect(() => {
    if (active >= questions.length && questions.length > 0) setActive(0);
  }, [active, questions.length]);

  const updateSelection = useCallback(
    (next: Partial<Selection>) => {
      setSelections((prev) => {
        const copy = prev.slice();
        copy[active] = { ...copy[active], ...next };
        return copy;
      });
    },
    [active],
  );

  const answers: (AskUserQuestionAnswer | null)[] = useMemo(
    () => questions.map((q, i) => buildAnswer(q, selections[i] ?? emptySelection())),
    [questions, selections],
  );
  const answeredCount = answers.filter((a) => a !== null).length;
  const allAnswered = answeredCount === questions.length;

  // Next unanswered question after ``from``, wrapping. The scan never
  // evaluates ``from`` itself, so the just-changed question can't
  // affect the result — the memoized answers are always current enough.
  const nextUnanswered = useCallback(
    (from: number): number | null => {
      for (let step = 1; step < questions.length; step++) {
        const i = (from + step) % questions.length;
        if (answers[i] === null) return i;
      }
      return null;
    },
    [questions.length, answers],
  );

  // Picking a concrete choice answers the question outright, so move
  // straight to the next open one — the green tab dot plus the new
  // prompt make the advance legible. "Other" stays put until typed,
  // and revising an already-answered question stays put too: yanking
  // the user away mid-correction would read as stolen focus.
  const selectChoice = useCallback(
    (choiceIndex: number) => {
      const wasAnswered = answers[active] !== null;
      const copy = selections.slice();
      copy[active] = { index: choiceIndex, other: "" };
      setSelections(copy);
      if (wasAnswered) return;
      const next = nextUnanswered(active);
      if (next !== null) setActive(next);
    },
    [active, answers, selections, nextUnanswered],
  );

  const advance = useCallback(() => {
    const next = nextUnanswered(active);
    if (next !== null) setActive(next);
  }, [active, nextUnanswered]);

  const handleSubmit = useCallback(async () => {
    if (!sessionId || locked || submitting) return;
    if (!allAnswered) {
      setError("Answer every question before submitting.");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      await adapter.submitAskUserQuestionResponse({
        sessionId,
        toolCallId: tc.id,
        responses: answers as AskUserQuestionAnswer[],
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Submit failed.");
    } finally {
      setSubmitting(false);
    }
  }, [adapter, sessionId, tc.id, answers, allAnswered, locked, submitting]);

  const handleCancel = useCallback(() => {
    if (!sessionId || locked) return;
    // Cancel = stop chat: pause the session.  The worker's
    // ask_user_question handler
    // sees the session.pause event and returns with ``cancelled: true``.
    void adapter.pauseSession({ sessionId }).catch(() => {
      // Best-effort; the user may press Esc again.
    });
  }, [adapter, sessionId, locked]);

  // Keyboard shortcuts: Esc cancels, Enter submits when all questions answered.
  useEffect(() => {
    if (locked) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        handleCancel();
      } else if (e.key === "Enter" && !e.shiftKey) {
        const tgt = e.target as HTMLElement | null;
        // Let textareas and the "Other" input consume Enter normally --
        // the global Enter only fires when focus is on the widget shell.
        if (tgt?.tagName === "TEXTAREA" || tgt?.tagName === "INPUT") return;
        e.preventDefault();
        void handleSubmit();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [handleCancel, handleSubmit, locked]);

  if (locked) {
    return <BatchAskLocked tc={tc} questions={questions} viewMode={viewMode} />;
  }

  const current = questions[active]!;
  const currentSel = selections[active] ?? emptySelection();

  return (
    <div
      className={cn(
        "rounded-md border border-border bg-background/80 shadow-sm",
        "max-w-2xl",
      )}
      role="group"
      aria-label="Questions for the user"
    >
      {/* Tab bar + close */}
      <div className="flex items-center justify-between border-b border-border px-3 pt-2">
        <div className="flex items-center gap-3 overflow-x-auto text-sm">
          {questions.map((_, i) => {
            const isActive = i === active;
            const answered = answers[i] !== null;
            return (
              <button
                key={i}
                type="button"
                onClick={() => setActive(i)}
                className={cn(
                  "relative pb-2 transition-colors whitespace-nowrap",
                  isActive
                    ? "text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                <span>Question {i + 1}</span>
                {answered && (
                  <span className="ml-1 text-[10px] text-emerald-500">●</span>
                )}
                {isActive && (
                  <span className="absolute inset-x-0 bottom-0 h-px bg-foreground" />
                )}
              </button>
            );
          })}
        </div>
        <button
          type="button"
          onClick={handleCancel}
          className="ml-2 rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          aria-label="Cancel clarification and stop chat"
          title="Esc — cancel and stop the chat"
        >
          <XIcon className="size-4" />
        </button>
      </div>

      {/* Prompt */}
      <div className="px-3 pt-3">
        <p className="text-sm text-foreground">{current.prompt}</p>
      </div>

      {/* Choices */}
      <div className="mt-3 px-1 pb-2">
        {(current.choices ?? []).map((choice, i) => (
          <ChoiceRow
            key={i}
            choice={choice}
            selected={currentSel.index === i}
            onSelect={() => selectChoice(i)}
          />
        ))}

        {(current.allow_other ?? true) && (
          <OtherRow
            selected={currentSel.index >= OTHER_INDEX_OFFSET}
            value={currentSel.other}
            onSelect={() =>
              updateSelection({ index: OTHER_INDEX_OFFSET })
            }
            onChange={(v) =>
              updateSelection({ index: OTHER_INDEX_OFFSET, other: v })
            }
            onCommit={() => {
              if (allAnswered) void handleSubmit();
              else advance();
            }}
          />
        )}
      </div>

      {/* Footer: progress + the one primary action */}
      <div className="flex items-center justify-between gap-3 border-t border-border px-3 py-2">
        <div className="min-w-0 text-xs text-muted-foreground">
          <span className="mr-2 tabular-nums">
            {answeredCount} of {questions.length} answered
          </span>
          <span className="text-muted-foreground/70">Esc to cancel</span>
          {error && (
            <span className="ml-2 text-destructive">{error}</span>
          )}
        </div>
        {allAnswered ? (
          <button
            type="button"
            disabled={submitting}
            onClick={() => void handleSubmit()}
            className={cn(
              "shrink-0 rounded-md bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground transition-colors",
              "hover:bg-primary/80",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
          >
            {submitting ? "Submitting…" : "Submit"}
          </button>
        ) : (
          <button
            type="button"
            onClick={advance}
            className={cn(
              "shrink-0 rounded-md border border-border bg-muted/40 px-4 py-1.5 text-sm font-medium text-foreground transition-colors",
              "hover:bg-muted",
            )}
          >
            Next question →
          </button>
        )}
      </div>
    </div>
  );
}

// ── Choice row ───────────────────────────────────────────────────────

function ChoiceRow({
  choice,
  selected,
  onSelect,
}: {
  choice: AskUserQuestionChoice;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "flex w-full items-start gap-3 rounded px-2 py-1.5 text-left transition-colors",
        selected ? "bg-muted" : "hover:bg-muted/40",
      )}
    >
      <Radio selected={selected} />
      <div className="min-w-0 flex-1">
        <div className="text-sm text-foreground">{choice.label}</div>
        {choice.description && (
          <div className="text-xs text-muted-foreground">
            {choice.description}
          </div>
        )}
      </div>
    </button>
  );
}

function OtherRow({
  selected,
  value,
  onSelect,
  onChange,
  onCommit,
}: {
  selected: boolean;
  value: string;
  onSelect: () => void;
  onChange: (v: string) => void;
  /** Enter in the input: submit when everything is answered, else advance. */
  onCommit: () => void;
}) {
  return (
    <div
      className={cn(
        "flex items-start gap-3 rounded px-2 py-1.5 transition-colors",
        selected ? "bg-muted" : "hover:bg-muted/40",
      )}
    >
      <button
        type="button"
        onClick={onSelect}
        className="flex shrink-0 items-center pt-0.5"
        aria-label="Choose Other"
      >
        <Radio selected={selected} />
      </button>
      <div className="min-w-0 flex-1">
        <div className="text-sm text-foreground">Other</div>
        <Input
          placeholder="Type your answer…"
          value={value}
          onFocus={onSelect}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            // isComposing: the Enter that finalizes an IME composition
            // must not double as an answer commit.
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing &&
              value.trim()
            ) {
              e.preventDefault();
              onCommit();
            }
          }}
          className="mt-0.5 h-7 px-0 text-xs"
        />
      </div>
    </div>
  );
}

function Radio({ selected }: { selected: boolean }) {
  return (
    <span
      aria-hidden
      className={cn(
        "mt-[3px] size-3.5 shrink-0 rounded-full border",
        selected
          ? "border-foreground bg-foreground"
          : "border-muted-foreground/60 bg-transparent",
      )}
    />
  );
}

// ── Batch: locked (after submit / during replay) ─────────────────────

function BatchAskLocked({
  tc,
  questions,
  viewMode,
}: {
  tc: ToolCallInfo;
  questions: AskUserQuestionQuestion[];
  viewMode: "simple" | "expert";
}) {
  const answers = tc.askUserQuestionAnswers;
  // Map answer question text back to the widget's question index so the
  // order matches the tabs (LLM-submitted order, not user navigation).
  const byPrompt = new Map(
    answers?.map((a) => [a.question, a]) ?? [],
  );

  const cancelled = !answers && tc.status !== "running";

  return (
    <div className="rounded-md border border-border bg-muted/20 px-3 py-2 text-sm max-w-2xl">
      <div className="mb-1 text-xs text-muted-foreground">
        {cancelled ? "Clarification cancelled" : "Clarification answered"}
      </div>
      <ul className="space-y-1.5">
        {questions.map((q, i) => {
          const a = byPrompt.get(q.prompt);
          return (
            <li key={i} className="text-sm">
              <div className="text-muted-foreground">
                <span className="text-muted-foreground/70">
                  Q{i + 1}.
                </span>{" "}
                {q.prompt}
              </div>
              {a ? (
                <div className="ml-5 text-foreground">
                  <span className="text-emerald-500">→</span>{" "}
                  {a.answer}
                  {/* "Off the menu" is operator signal, not end-user
                      signal: it tells whoever reviews the session that
                      the offered options did not fit. */}
                  {a.is_other && viewMode === "expert" && (
                    <span className="ml-1 text-[10px] text-muted-foreground/70">
                      (other)
                    </span>
                  )}
                </div>
              ) : (
                <div className="ml-5 text-muted-foreground/60 italic">
                  — no answer —
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Clarify tool widget -- tabs for each question, radio choices with
// labels + descriptions, an optional "Other" free-form row, and a single
// Submit that batches every answer back to the worker.  Esc pauses the
// session (= user chose to stop the chat instead of answering).

import { useCallback, useEffect, useMemo, useState } from "react";
import { XIcon } from "lucide-react";
import { cn } from "../../../lib/utils";
import { Input } from "../../ui/input";
import { useAgentChatAdapterContext } from "../../../adapter-context";
import { parseArgs } from "./shared";
import type { ToolCallInfo } from "../../../types";
import type {
  ClarifyAnswer,
  ClarifyArgs,
  ClarifyChoice,
  ClarifyQuestion,
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

function buildAnswer(q: ClarifyQuestion, sel: Selection): ClarifyAnswer | null {
  if (sel.index < 0) return null;
  if (sel.index >= OTHER_INDEX_OFFSET) {
    const text = sel.other.trim();
    if (!text) return null;
    return { question: q.prompt, answer: text, is_other: true };
  }
  const choice = q.choices?.[sel.index];
  if (!choice) return null;
  return { question: q.prompt, answer: choice.label, is_other: false };
}

export function ClarifyToolBlock({ tc }: { tc: ToolCallInfo }) {
  const { adapter, sessionId } = useAgentChatAdapterContext();
  const args = useMemo(() => parseArgs<ClarifyArgs>(tc.args), [tc.args]);
  const questions = args?.questions ?? [];

  const [active, setActive] = useState(0);
  const [selections, setSelections] = useState<Selection[]>(() =>
    questions.map(emptySelection),
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const locked = tc.clarifyAnswers !== undefined || tc.status !== "running";

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

  const answers: (ClarifyAnswer | null)[] = useMemo(
    () => questions.map((q, i) => buildAnswer(q, selections[i] ?? emptySelection())),
    [questions, selections],
  );
  const allAnswered = answers.every((a) => a !== null);

  const handleSubmit = useCallback(async () => {
    if (!sessionId || locked || submitting) return;
    if (!allAnswered) {
      setError("Answer every question before submitting.");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      await adapter.submitClarifyResponse({
        sessionId,
        toolCallId: tc.id,
        responses: answers as ClarifyAnswer[],
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Submit failed.");
    } finally {
      setSubmitting(false);
    }
  }, [adapter, sessionId, tc.id, answers, allAnswered, locked, submitting]);

  const handleCancel = useCallback(() => {
    if (!sessionId || locked) return;
    // Cancel = stop chat: pause the session.  The worker's clarify handler
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

  if (questions.length === 0) {
    return (
      <div className="rounded-md border border-destructive/50 bg-destructive/5 px-3 py-2 text-xs text-destructive">
        clarify: no questions provided
      </div>
    );
  }

  if (locked) {
    return <ClarifyLocked tc={tc} questions={questions} />;
  }

  const current = questions[active];
  const currentSel = selections[active] ?? emptySelection();

  return (
    <div
      className={cn(
        "rounded-md border border-border bg-background/80 shadow-sm",
        "max-w-2xl",
      )}
      role="group"
      aria-label="Clarifying questions"
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
                {answered && !isActive && (
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
            onSelect={() => updateSelection({ index: i })}
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
          />
        )}
      </div>

      {/* Submit */}
      <div className="border-t border-border px-3 py-2">
        <button
          type="button"
          disabled={!allAnswered || submitting}
          onClick={() => void handleSubmit()}
          className={cn(
            "w-full rounded border border-border px-2 py-1.5 text-left text-sm ",
            "text-muted-foreground hover:bg-muted/40",
            "disabled:cursor-not-allowed disabled:opacity-60",
          )}
        >
          <span className="mr-2 text-muted-foreground/70">
            {answers.filter((a) => a !== null).length}
          </span>
          {submitting
            ? "Submitting…"
            : allAnswered
              ? "Submit answers"
              : `Answer ${questions.length - answers.filter((a) => a !== null).length} more to submit`}
        </button>
        <div className="mt-1 flex items-center justify-between text-[10px] text-muted-foreground/70">
          <span>Esc to cancel</span>
          {error && <span className="text-destructive">{error}</span>}
        </div>
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
  choice: ClarifyChoice;
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
}: {
  selected: boolean;
  value: string;
  onSelect: () => void;
  onChange: (v: string) => void;
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

// ── Locked (after submit / during replay) ────────────────────────────

function ClarifyLocked({
  tc,
  questions,
}: {
  tc: ToolCallInfo;
  questions: ClarifyQuestion[];
}) {
  const answers = tc.clarifyAnswers;
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
                  {a.is_other && (
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

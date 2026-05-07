// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renders consult_expert tool calls with thumbs-up / thumbs-down
// feedback buttons.  Thumbs-up posts immediately; thumbs-down opens a
// comment form so the user can record why the response was
// unsatisfactory.  Submission hits
// POST /v1/sessions/{id}/events/{event_id}/feedback (where event_id
// is the id of the expert.result turn being rated) which emits
// EXPERT_ENDORSE or EXPERT_OVERRIDE into the event log.

import { useState } from "react";
import { ChevronRightIcon, ThumbsDownIcon, ThumbsUpIcon } from "lucide-react";
import { cn } from "../../../lib/utils";
import { Textarea } from "../../ui/textarea";
import { Button } from "../../ui/button";
import { parseArgs } from "./shared";
import type { ToolCallInfo } from "../../../types";
import type { AgentChatExpertFeedbackRating } from "../../../types";
import { useAgentChatAdapterContext } from "../../../adapter-context";

// Keep in sync with _MAX_REASON_LENGTH in api/routes/feedback.py.
const MAX_REASON_LENGTH = 500;

interface ExpertArgs {
  expert?: string;
  question?: string;
  prompt?: string;
}

interface ExpertResult {
  summary?: string;
  response?: string;
  content?: string;
  error?: string | null;
}

export function ExpertToolBlock({ tc }: { tc: ToolCallInfo }) {
  const [expanded, setExpanded] = useState(false);
  const { adapter, sessionId } = useAgentChatAdapterContext();
  // Optimistic marker: set the moment the user clicks, cleared when the
  // server responds.  Authoritative rating arrives via the SSE stream as
  // tc.expertFeedback; we prefer it once present.
  const [pending, setPending] = useState<AgentChatExpertFeedbackRating | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reasonDraft, setReasonDraft] = useState<string | null>(null);

  const rating = tc.expertFeedback?.rating ?? pending;
  const alreadyRated = tc.expertFeedback !== undefined;
  const canRate =
    tc.expertResultEventId !== undefined &&
    sessionId !== null &&
    adapter.submitExpertFeedback !== undefined &&
    tc.status !== "running";

  const submit = async (next: AgentChatExpertFeedbackRating, reason?: string) => {
    if (
      sessionId === null ||
      tc.expertResultEventId === undefined ||
      adapter.submitExpertFeedback === undefined
    ) return;
    setPending(next);
    setError(null);
    try {
      await adapter.submitExpertFeedback({
        sessionId,
        expertResultEventId: tc.expertResultEventId,
        rating: next,
        reason,
      });
      setReasonDraft(null);
    } catch (e) {
      setPending(null);
      setError(e instanceof Error ? e.message : "Failed to submit feedback");
    }
  };

  const handleRate = (next: AgentChatExpertFeedbackRating) => {
    if (!canRate || alreadyRated || pending !== null) return;
    if (next === "down") {
      setReasonDraft("");
      return;
    }
    void submit("up");
  };

  const args = parseArgs<ExpertArgs>(tc.args) ?? {};
  const expertName = args.expert ?? null;
  const question = args.question ?? args.prompt ?? "";
  const result = parseExpertResult(tc.result);
  const failed = Boolean(result?.error);

  return (
    <div className="space-y-1">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className={cn(
          "flex w-fit max-w-full items-center gap-1.5 rounded-md px-0 py-0.5",
          "text-sm text-muted-foreground hover:text-foreground transition-colors"
        )}
      >
        <ChevronRightIcon
          className={cn(
            "size-3 shrink-0 transition-transform duration-150",
            expanded && "rotate-90",
          )}
        />
        <span className="font-semibold text-foreground">Consulted expert</span>
        {expertName && (
          <span className="text-muted-foreground">· {expertName}</span>
        )}
        {failed && <span className="text-red-500">· failed</span>}
      </button>

      {result?.summary && !expanded && (
        <div className="ml-6 rounded-md border-l-2 border-primary/50 bg-muted/30 px-3 py-2 text-sm text-foreground/90">
          {result.summary}
        </div>
      )}

      {expanded && (
        <div className="ml-6 mt-0.5 space-y-1.5 text-sm">
          {question && (
            <ExpertDetail label="Question" content={question} />
          )}
          {result?.summary && (
            <ExpertDetail
              label={failed ? "Error" : "Response"}
              content={result.summary}
              tone={failed ? "error" : "default"}
            />
          )}
        </div>
      )}

      {canRate && (
        <div className="ml-6 mt-1 space-y-1.5">
          <div className="flex items-center gap-1 text-xs text-muted-foreground">
            <span>Rate this expert's response:</span>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label="Good response"
              title={alreadyRated && rating === "up" ? "Good response (recorded)" : "Good response"}
              disabled={pending !== null || (alreadyRated && rating !== "up")}
              onClick={() => handleRate("up")}
              className={cn(
                rating === "up" ? "text-foreground" : "text-muted-foreground/60",
              )}
            >
              <ThumbsUpIcon className="size-3.5" />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label="Poor response"
              title={alreadyRated && rating === "down" ? "Poor response (recorded)" : "Poor response"}
              disabled={
                pending !== null ||
                reasonDraft !== null ||
                (alreadyRated && rating !== "down")
              }
              onClick={() => handleRate("down")}
              className={cn(
                rating === "down" ? "text-foreground" : "text-muted-foreground/60",
              )}
            >
              <ThumbsDownIcon className="size-3.5" />
            </Button>
            {alreadyRated && tc.expertFeedback?.reason && (
              <span
                className="text-muted-foreground/70 truncate max-w-xs"
                title={tc.expertFeedback.reason}
              >
                · "{tc.expertFeedback.reason}"
              </span>
            )}
            {error && <span className="text-red-500 ml-1">{error}</span>}
          </div>

          {reasonDraft !== null && !alreadyRated && (
            <ReasonForm
              initialValue={reasonDraft}
              busy={pending !== null}
              onSubmit={(reason) => void submit("down", reason)}
              onCancel={() => {
                setReasonDraft(null);
                setError(null);
              }}
            />
          )}
        </div>
      )}
    </div>
  );
}

function parseExpertResult(result: string | undefined): ExpertResult | null {
  if (!result) return null;
  const parsed = parseArgs<ExpertResult>(result);
  if (parsed) {
    return {
      ...parsed,
      summary: parsed.summary ?? parsed.response ?? parsed.content,
    };
  }
  return { summary: result };
}

function ExpertDetail({
  label,
  content,
  tone = "default",
}: {
  label: string;
  content: string;
  tone?: "default" | "error";
}) {
  return (
    <div
      className={cn(
        "rounded-md border-l-2 px-3 py-2",
        tone === "error"
          ? "border-red-500/50 bg-red-500/5"
          : "border-primary/50 bg-muted/30",
      )}
    >
      <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground/70">
        {label}
      </div>
      <div className="whitespace-pre-wrap wrap-break-word text-foreground/90">
        {content}
      </div>
    </div>
  );
}

function ReasonForm({
  initialValue,
  busy,
  onSubmit,
  onCancel,
}: {
  initialValue: string;
  busy: boolean;
  onSubmit: (reason: string) => void;
  onCancel: () => void;
}) {
  // State is local so keystrokes don't re-render the parent tool block.
  const [value, setValue] = useState(initialValue);
  const trimmed = value.trim();
  const charsLeft = MAX_REASON_LENGTH - value.length;

  const submit = () => {
    if (trimmed && !busy) onSubmit(trimmed);
  };

  return (
    <div className="rounded-md border border-border bg-muted/30 p-2 space-y-1.5">
      <label className="text-xs text-muted-foreground" htmlFor="expert-reason">
        What was wrong with the response?
      </label>
      <Textarea
        id="expert-reason"
        autoFocus
        rows={3}
        value={value}
        maxLength={MAX_REASON_LENGTH}
        placeholder="e.g. wrong WHERE clause, missed an edge case, hallucinated a column name…"
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            submit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            onCancel();
          }
        }}
        className="text-xs min-h-12"
        disabled={busy}
      />
      <div className="flex items-center justify-between gap-2">
        <span
          className={cn(
            "text-[10px] tabular-nums",
            charsLeft < 50 ? "text-amber-500" : "text-muted-foreground/60",
          )}
        >
          {charsLeft} characters left
        </span>
        <div className="flex items-center gap-1.5">
          <Button
            type="button"
            variant="ghost"
            size="xs"
            onClick={onCancel}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="default"
            size="xs"
            onClick={submit}
            disabled={busy || !trimmed}
          >
            {busy ? "Sending…" : "Send feedback"}
          </Button>
        </div>
      </div>
    </div>
  );
}

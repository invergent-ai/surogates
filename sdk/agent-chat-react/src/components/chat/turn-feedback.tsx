// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renders thumbs-up / thumbs-down feedback under an assistant message's
// final text answer.  Thumbs-up posts immediately; thumbs-down opens an
// inline comment form so the user can record why the response was
// unsatisfactory.  Submission hits
// POST /v1/sessions/{id}/events/{event_id}/feedback (where event_id is
// the id of the llm.response turn being rated) which emits
// USER_FEEDBACK into the event log.

import { useState } from "react";
import { ThumbsDownIcon, ThumbsUpIcon } from "lucide-react";
import { cn } from "../../lib/utils";
import { Textarea } from "../ui/textarea";
import { Button } from "../ui/button";
import type {
  AgentChatExpertFeedbackRating,
  ChatMessage as ChatMessageType,
} from "../../types";
import { useAgentChatAdapterContext } from "../../adapter-context";

// Keep in sync with _MAX_REASON_LENGTH in api/routes/feedback.py.
const MAX_REASON_LENGTH = 500;

export function TurnFeedback({ msg }: { msg: ChatMessageType }) {
  const { adapter, sessionId } = useAgentChatAdapterContext();
  // Optimistic marker: set the moment the user clicks, cleared on
  // success or rejected error.  Authoritative rating arrives via SSE
  // as msg.userFeedback; we prefer it once present.
  const [pending, setPending] = useState<AgentChatExpertFeedbackRating | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [reasonDraft, setReasonDraft] = useState<string | null>(null);

  const llmResponseEventId = msg.llmResponseEventId;
  const canRate =
    llmResponseEventId !== undefined &&
    msg.status === "complete" &&
    sessionId !== null &&
    adapter.submitUserFeedback !== undefined;

  if (!canRate) return null;

  const rating = msg.userFeedback?.rating ?? pending;
  const alreadyRated = msg.userFeedback !== undefined;

  const submit = async (
    next: AgentChatExpertFeedbackRating,
    reason?: string,
  ) => {
    if (
      sessionId === null ||
      llmResponseEventId === undefined ||
      adapter.submitUserFeedback === undefined
    ) return;
    setPending(next);
    setError(null);
    try {
      await adapter.submitUserFeedback({
        sessionId,
        llmResponseEventId,
        rating: next,
        ...(reason ? { reason } : {}),
      });
      setReasonDraft(null);
    } catch (e) {
      setPending(null);
      setError(e instanceof Error ? e.message : "Failed to submit feedback");
    }
  };

  const handleRate = (next: AgentChatExpertFeedbackRating) => {
    if (alreadyRated || pending !== null) return;
    if (next === "down") {
      setReasonDraft("");
      return;
    }
    void submit("up");
  };

  return (
    <div className="mt-1 space-y-1.5">
      <div className="flex items-center gap-1 text-xs text-muted-foreground">
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Good response"
          title={
            alreadyRated && rating === "up"
              ? "Good response (recorded)"
              : "Good response"
          }
          disabled={pending !== null || alreadyRated}
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
          title={
            alreadyRated && rating === "down"
              ? "Poor response (recorded)"
              : "Poor response"
          }
          disabled={pending !== null || reasonDraft !== null || alreadyRated}
          onClick={() => handleRate("down")}
          className={cn(
            rating === "down" ? "text-foreground" : "text-muted-foreground/60",
          )}
        >
          <ThumbsDownIcon className="size-3.5" />
        </Button>
        {alreadyRated && msg.userFeedback?.reason && (
          <span
            className="text-muted-foreground/70 truncate max-w-xs"
            title={msg.userFeedback.reason}
          >
            · "{msg.userFeedback.reason}"
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
  // State is local so keystrokes don't re-render the parent block.
  const [value, setValue] = useState(initialValue);
  const trimmed = value.trim();
  const charsLeft = MAX_REASON_LENGTH - value.length;

  const submit = () => {
    if (trimmed && !busy) onSubmit(trimmed);
  };

  return (
    <div className="rounded-md border border-border bg-muted/30 p-2 space-y-1.5">
      <label
        className="text-xs text-muted-foreground"
        htmlFor="turn-feedback-reason"
      >
        What was wrong with the response?
      </label>
      <Textarea
        id="turn-feedback-reason"
        autoFocus
        rows={3}
        value={value}
        maxLength={MAX_REASON_LENGTH}
        placeholder="e.g. wrong answer, missed a constraint, hallucinated a fact…"
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

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Error bubble rendered inline when a session fails or an assistant
// message lands with status="error".  Pairs the classifier's friendly
// title with a collapsible detail block and an optional Retry action.
//
// Used both as a standalone timeline entry (when session.fail arrives
// with no preceding assistant slot) and inline below an assistant
// message that failed mid-turn.

import { memo, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  CreditCard,
  RefreshCw,
} from "lucide-react";
import { Button } from "../ui/button";
import { cn } from "../../lib/utils";
import { useAgentChatAdapterContext } from "../../adapter-context";
import type { ErrorInfo } from "../../types";

interface ErrorMessageProps {
  errorInfo: ErrorInfo;
  /** Retry handler — omit or leave undefined to hide the Retry button.
   *  The button is also hidden when ``errorInfo.retryable`` is false. */
  onRetry?: () => Promise<void>;
  /** Local-only dismiss (hides the bubble without touching server state). */
  onDismiss?: () => void;
  className?: string;
}

export const ErrorMessage = memo(function ErrorMessage({
  errorInfo,
  onRetry,
  onDismiss,
  className,
}: ErrorMessageProps) {
  const [detailOpen, setDetailOpen] = useState(false);
  const [retryPending, setRetryPending] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  if (errorInfo.insufficientCredits) {
    return (
      <InsufficientCreditsCard
        detail={errorInfo.detail}
        onDismiss={onDismiss}
        className={className}
      />
    );
  }

  const showRetry = errorInfo.retryable && !!onRetry;

  const handleRetry = async () => {
    if (!onRetry) return;
    setRetryPending(true);
    setRetryError(null);
    try {
      await onRetry();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error("Retry failed", err);
      setRetryError(msg || "Retry failed.");
    } finally {
      setRetryPending(false);
    }
  };

  return (
    <div
      role="alert"
      className={cn(
        "flex w-full gap-3 rounded-none border-l-4 border-destructive bg-destructive/5 p-4",
        className,
      )}
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="text-sm font-semibold text-destructive">
          {errorInfo.title}
        </div>

        {errorInfo.detail && (
          <button
            type="button"
            onClick={() => setDetailOpen((open) => !open)}
            className="flex items-center gap-1 self-start text-xs text-muted-foreground hover:text-foreground"
            aria-expanded={detailOpen}
          >
            {detailOpen ? (
              <ChevronDown className="size-3" />
            ) : (
              <ChevronRight className="size-3" />
            )}
            {detailOpen ? "Hide details" : "Show details"}
          </button>
        )}

        {detailOpen && errorInfo.detail && (
          <pre className="overflow-x-auto rounded-none border border-destructive/20 bg-background p-2 font-mono text-xs whitespace-pre-wrap wrap-break-word text-muted-foreground">
            {errorInfo.detail}
          </pre>
        )}

        {(showRetry || onDismiss) && (
          <div className="flex gap-2 pt-1">
            {showRetry && (
              <Button
                size="xs"
                variant="secondary"
                onClick={handleRetry}
                disabled={retryPending}
              >
                <RefreshCw
                  className={cn("size-3", retryPending && "animate-spin")}
                />
                {retryPending ? "Retrying…" : "Retry"}
              </Button>
            )}
            {onDismiss && (
              <Button size="xs" variant="ghost" onClick={onDismiss}>
                Dismiss
              </Button>
            )}
          </div>
        )}

        {retryError && (
          <div className="pt-1 text-xs text-destructive" role="status">
            Retry failed: {retryError}
          </div>
        )}
      </div>
    </div>
  );
});


/** Pull a friendly "N tokens" figure out of the proxy's 402 payload
 * (``{"requested": 14769, "available": -31725}``), best-effort. */
function parseRequestedTokens(detail: string): number | null {
  const m = detail.match(/["']?requested["']?\s*[:=]\s*(\d+)/);
  return m ? Number(m[1]) : null;
}

interface InsufficientCreditsCardProps {
  detail: string;
  onDismiss?: () => void;
  className?: string;
}

const InsufficientCreditsCard = memo(function InsufficientCreditsCard({
  detail,
  onDismiss,
  className,
}: InsufficientCreditsCardProps) {
  const { onOpenBilling } = useAgentChatAdapterContext();
  const requested = parseRequestedTokens(detail);
  return (
    <div
      role="alert"
      className={cn(
        "flex w-full gap-3 rounded-md border border-amber-500/30 bg-amber-500/5 p-4",
        className,
      )}
    >
      <CreditCard className="mt-0.5 size-5 shrink-0 text-amber-600 dark:text-amber-400" />
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="text-sm font-semibold text-foreground">
          You're out of credits
        </div>
        <p className="text-sm text-muted-foreground">
          This run was blocked by the token-credit limit
          {requested ? ` (it needed ~${requested.toLocaleString()} tokens)` : ""}.
          Buy additional credits or upgrade your plan to keep your agents
          running.
        </p>
        <div className="flex flex-wrap gap-2 pt-1">
          {onOpenBilling && (
            <Button size="sm" onClick={onOpenBilling}>
              <CreditCard className="size-4" />
              Go to Billing
            </Button>
          )}
          {onDismiss && (
            <Button size="sm" variant="ghost" onClick={onDismiss}>
              Dismiss
            </Button>
          )}
        </div>
      </div>
    </div>
  );
});

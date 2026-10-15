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
import { Alert, AlertDescription, AlertTitle } from "@invergent/ui-sdk/alert";
import { Button } from "@invergent/ui-sdk/button";
import { TextLink } from "@invergent/ui-sdk/text-link";
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
    <Alert variant="destructive" className={cn("gap-y-2", className)}>
      <AlertTriangle />
      <AlertTitle>{errorInfo.title}</AlertTitle>
      <AlertDescription className="flex min-w-0 flex-col items-start gap-2">
        {errorInfo.detail && (
          <TextLink variant="muted" size="sm" asChild>
            <button
              type="button"
              onClick={() => setDetailOpen((open) => !open)}
              aria-expanded={detailOpen}
            >
              {detailOpen ? <ChevronDown /> : <ChevronRight />}
              {detailOpen ? "Hide details" : "Show details"}
            </button>
          </TextLink>
        )}

        {detailOpen && errorInfo.detail && (
          <pre className="w-full overflow-x-auto rounded-[6px] border border-destructive/20 bg-background p-2 font-mono text-xs whitespace-pre-wrap wrap-break-word text-muted-foreground">
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
      </AlertDescription>
    </Alert>
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
    <Alert variant="warning" className={cn("gap-y-2", className)}>
      <CreditCard />
      <AlertTitle>You're out of credits</AlertTitle>
      <AlertDescription className="flex flex-col items-start gap-2">
        <p>
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
      </AlertDescription>
    </Alert>
  );
});

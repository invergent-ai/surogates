import { MousePointer2Icon, RotateCcwIcon, XIcon } from "lucide-react";
import { useState } from "react";
import { Button } from "../ui/button";
import { ConfirmDialog } from "../ui/confirm-dialog";
import type { AgentChatAdapter } from "../../types";

type BrowserControlAdapter = Pick<
  AgentChatAdapter,
  "acquireBrowserControl" | "releaseBrowserControl" | "closeBrowserSession"
>;

interface BrowserControlBarProps {
  sessionId: string;
  hasControl: boolean;
  adapter: BrowserControlAdapter;
  onControlAcquired?: () => void;
  onControlReleased?: () => void;
  /**
   * Optional handler invoked AFTER the user has confirmed the close
   * action AND the backend session-close call (if any) has succeeded.
   * The button is only rendered when this prop is supplied so existing
   * embedders that don't want a close affordance opt in explicitly.
   * If the adapter provides closeBrowserSession, it is awaited before
   * onClose is called — failures abort the close and surface an error
   * message on the bar.
   */
  onClose?: () => void;
}

export function BrowserControlBar({
  sessionId,
  hasControl,
  adapter,
  onControlAcquired,
  onControlReleased,
  onClose,
}: BrowserControlBarProps) {
  const [pending, setPending] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggleControl() {
    if (!sessionId || pending) return;
    setPending(true);
    setError(null);
    try {
      if (hasControl) {
        await adapter.releaseBrowserControl(sessionId);
        onControlReleased?.();
      } else {
        await adapter.acquireBrowserControl(sessionId);
        onControlAcquired?.();
      }
    } catch (nextError) {
      setError((nextError as Error).message);
    } finally {
      setPending(false);
    }
  }

  async function handleConfirmClose() {
    setError(null);
    try {
      // Release-before-close lets the agent reclaim control immediately
      // in the event that closeBrowserSession is not available (e.g.,
      // older ops adapter). Release errors are non-fatal — the 60s TTL
      // on the harness reaps the lease independently.
      if (hasControl) {
        try {
          await adapter.releaseBrowserControl(sessionId);
          onControlReleased?.();
        } catch (releaseError) {
          console.error(
            "Failed to release browser control before close",
            releaseError,
          );
        }
      }
      if (adapter.closeBrowserSession) {
        await adapter.closeBrowserSession(sessionId);
      }
      setConfirmOpen(false);
      onClose?.();
    } catch (nextError) {
      // Keep the dialog open so the user can see the error and retry
      // or cancel.
      setError((nextError as Error).message);
      throw nextError;
    }
  }

  return (
    <div className="flex min-h-11 items-center gap-2 border-t border-line bg-card px-3 py-2">
      <Button
        type="button"
        size="xs"
        variant={hasControl ? "secondary" : "default"}
        disabled={!sessionId || pending}
        onClick={() => void toggleControl()}
      >
        {hasControl ? (
          <RotateCcwIcon className="size-3" aria-hidden="true" />
        ) : (
          <MousePointer2Icon className="size-3" aria-hidden="true" />
        )}
        {hasControl ? "Return control" : "Take control"}
      </Button>
      {onClose && (
        <Button
          type="button"
          size="xs"
          variant="secondary"
          disabled={!sessionId}
          onClick={() => {
            setError(null);
            setConfirmOpen(true);
          }}
        >
          <XIcon className="size-3" aria-hidden="true" />
          Close
        </Button>
      )}
      <span className="ml-auto flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <span className="size-1.5 rounded-full bg-red-500" aria-hidden="true" />
        Live
      </span>
      {error && (
        <span className="max-w-40 truncate text-[11px] text-destructive">
          {error}
        </span>
      )}
      <ConfirmDialog
        open={confirmOpen}
        title="Close browser session?"
        description={
          adapter.closeBrowserSession
            ? "This permanently shuts down the browser sandbox for this session. The agent will lose access to the page until it re-opens a browser. This cannot be undone."
            : "This hides the browser panel. The sandbox stays running so the agent can keep using it."
        }
        confirmLabel="Close browser"
        variant="destructive"
        confirmIcon={<XIcon className="size-3.5" aria-hidden="true" />}
        onConfirm={handleConfirmClose}
        onCancel={() => setConfirmOpen(false)}
      />
    </div>
  );
}

import { useState } from "react";
import type { AgentChatAdapter } from "../../types";

/**
 * Take/return browser control, and the close-with-confirm flow.
 *
 * Lifted verbatim out of `BrowserControlBar` when the shell absorbed the
 * pane's chrome: the bar's markup went, but its adapter calls, pending and
 * error state, and release-before-close ordering are behaviour, not
 * presentation. The shell renders no dialogs, so the consumer keeps the
 * ConfirmDialog and drives it from `confirmOpen`.
 */

export type BrowserControlAdapter = Pick<
  AgentChatAdapter,
  "acquireBrowserControl" | "releaseBrowserControl" | "closeBrowserSession"
>;

interface UseBrowserControlOptions {
  sessionId: string;
  hasControl: boolean;
  adapter: BrowserControlAdapter;
  onControlAcquired?: () => void;
  onControlReleased?: () => void;
  /**
   * Invoked AFTER the user confirms the close AND the backend
   * session-close call (if any) has succeeded. Failures abort the close and
   * surface an error instead.
   */
  onClose?: () => void;
}

export function useBrowserControl({
  sessionId,
  hasControl,
  adapter,
  onControlAcquired,
  onControlReleased,
  onClose,
}: UseBrowserControlOptions) {
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

  return {
    pending,
    error,
    confirmOpen,
    setConfirmOpen,
    toggleControl,
    handleConfirmClose,
  };
}

import { MousePointer2Icon, RotateCcwIcon, XIcon } from "lucide-react";
import { useState } from "react";
import { Button } from "../ui/button";
import type { AgentChatAdapter } from "../../types";

type BrowserControlAdapter = Pick<
  AgentChatAdapter,
  "acquireBrowserControl" | "releaseBrowserControl"
>;

interface BrowserControlBarProps {
  sessionId: string;
  hasControl: boolean;
  adapter: BrowserControlAdapter;
  onControlAcquired?: () => void;
  onControlReleased?: () => void;
  /**
   * Optional handler invoked when the user clicks the "Close" button.
   * The button is only rendered when this prop is supplied so existing
   * embedders that don't want a close affordance opt in explicitly.
   * If control is held when the user closes, release is awaited first
   * so the agent can reclaim the browser cleanly.
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
  const [closePending, setClosePending] = useState(false);
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

  async function handleClose() {
    if (!sessionId || closePending) return;
    setClosePending(true);
    setError(null);
    try {
      // Release-before-close lets the agent reclaim control immediately;
      // failures here are non-fatal — we still signal close so the
      // parent can hide the pane.
      if (hasControl) {
        try {
          await adapter.releaseBrowserControl(sessionId);
          onControlReleased?.();
        } catch (releaseError) {
          // Log but don't block close; the harness's TTL reaps the
          // lease independently within 60s.
          console.error(
            "Failed to release browser control before close",
            releaseError,
          );
        }
      }
      onClose?.();
    } finally {
      setClosePending(false);
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
          disabled={!sessionId || closePending}
          onClick={() => void handleClose()}
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
    </div>
  );
}

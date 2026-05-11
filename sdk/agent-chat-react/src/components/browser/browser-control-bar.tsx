import { MousePointer2Icon, RotateCcwIcon } from "lucide-react";
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
}

export function BrowserControlBar({
  sessionId,
  hasControl,
  adapter,
  onControlAcquired,
}: BrowserControlBarProps) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggleControl() {
    if (!sessionId || pending) return;
    setPending(true);
    setError(null);
    try {
      if (hasControl) {
        await adapter.releaseBrowserControl(sessionId);
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

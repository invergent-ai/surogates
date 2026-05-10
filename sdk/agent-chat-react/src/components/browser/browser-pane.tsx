import { useMemo } from "react";
import { ZapIcon } from "lucide-react";
import { BrowserControlBar } from "./browser-control-bar";
import { BrowserLiveView } from "./browser-live-view";
import { BrowserStatusDot } from "./browser-status-dot";
import type { AgentChatAdapter, AgentChatBrowserState } from "../../types";

type BrowserPaneAdapter = Pick<
  AgentChatAdapter,
  "browserLiveViewUrl" | "acquireBrowserControl" | "releaseBrowserControl"
>;

interface BrowserPaneProps {
  sessionId: string;
  state: AgentChatBrowserState;
  adapter: BrowserPaneAdapter;
}

export function BrowserPane({ sessionId, state, adapter }: BrowserPaneProps) {
  const hasLiveViewAdapter =
    typeof adapter.browserLiveViewUrl === "function";
  const hasControlAdapter =
    typeof adapter.acquireBrowserControl === "function" &&
    typeof adapter.releaseBrowserControl === "function";
  const liveViewUrl = useMemo(() => {
    if (!hasLiveViewAdapter) return "";
    return adapter.browserLiveViewUrl(sessionId);
  }, [adapter, hasLiveViewAdapter, sessionId]);
  const hasLiveView = state.status !== "provisioning" && state.status !== "closed";

  return (
    <div
      data-testid="browser-pane"
      className="flex h-full min-h-0 flex-col bg-background"
    >
      <header className="flex min-h-10 items-center gap-2 border-b border-line bg-card px-3 text-xs text-foreground">
        <ZapIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
        <span className="font-medium">Browser</span>
        <BrowserStatusDot status={state.status} />
        {state.controlOwner && (
          <span className="min-w-0 truncate text-amber-500">
            {state.controlOwner} has control
          </span>
        )}
      </header>
      <div className="min-h-0 flex-1 bg-black">
        {state.status === "provisioning" ? (
          <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
            Starting browser...
          </div>
        ) : state.status === "closed" ? (
          <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
            Browser closed.
          </div>
        ) : liveViewUrl ? (
          <BrowserLiveView src={liveViewUrl} />
        ) : (
          <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
            Browser live view is unavailable.
          </div>
        )}
      </div>
      {hasLiveView && hasControlAdapter && (
        <BrowserControlBar
          sessionId={sessionId}
          hasControl={state.status === "user-control"}
          adapter={adapter}
        />
      )}
    </div>
  );
}

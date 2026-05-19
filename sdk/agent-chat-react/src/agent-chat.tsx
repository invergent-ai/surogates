import { useCallback, useEffect, useState } from "react";
import { AgentChatAdapterProvider } from "./adapter-context";
import { BrowserPane } from "./components/browser/browser-pane";
import { ChatThread } from "./components/chat/chat-thread";
import { TooltipProvider } from "./components/ui/tooltip";
import { WorkspacePanel } from "./components/workspace/workspace-panel";
import { cn } from "./lib/utils";
import { isScheduledRunSession } from "./lib/sessions";
import { useAgentChatRuntime } from "./runtime/use-agent-chat-runtime";
import type {
  AgentChatAdapter,
  AgentChatMessage,
} from "./types";
import type { ChatComposerError } from "./components/chat/chat-composer";

export interface AgentChatProps {
  adapter: AgentChatAdapter;
  agentId?: string;
  sessionId: string | null;
  onSessionChange?: (sessionId: string) => void;
  onFileSelect?: (path: string) => void;
  onMessagesChange?: (messages: AgentChatMessage[]) => void;
  disabled?: boolean;
  /**
   * Called when the composer rejects a file selection before sending —
   * size/count caps, accept-pattern misses.  Host apps wire this to
   * their toast system; the SDK does not surface these on its own.
   */
  onComposerError?: (err: ChatComposerError) => void;
}

// CSS variable controlling the desktop right-stack width. Inlined as a style
// so it stays component-local; arbitrary-value Tailwind classes read it.
const RIGHT_STACK_STYLE = {
  ["--right-stack-w" as string]: "440px",
} as React.CSSProperties;

export function AgentChat({
  adapter,
  agentId,
  sessionId,
  onSessionChange,
  onFileSelect,
  onMessagesChange,
  disabled,
  onComposerError,
}: AgentChatProps) {
  const [workspacePath, setWorkspacePath] = useState<string | null>(null);
  // On phones the chat and workspace panes don't fit side-by-side. A
  // segmented control at the top of the layout swaps between them. On md+
  // both are visible and the toggle is hidden.
  const [mobileView, setMobileView] = useState<"chat" | "workspace">("chat");
  const runtime = useAgentChatRuntime({
    adapter,
    agentId,
    sessionId,
    onSessionChange,
  });
  const isReadOnlySession = isScheduledRunSession(runtime.session);
  const effectiveDisabled = disabled || isReadOnlySession;
  const disabledReason = isReadOnlySession
    ? "Scheduled run is read-only"
    : undefined;
  const browserState = runtime.state.browser;
  // Hide the BrowserPane when the browser is closed or when the session
  // itself is finished. Old sessions replay the `browser.provisioned`
  // event but the server doesn't always replay the matching `destroyed`
  // event for sessions whose browser was torn down with the session, so
  // we'd otherwise render an empty "preview unavailable" panel for any
  // historical session that ever used the browser.
  const sessionFinished =
    runtime.session?.status === "completed" ||
    runtime.session?.status === "failed";
  const hasBrowserPanel =
    browserState !== null &&
    browserState.status !== "closed" &&
    !sessionFinished &&
    sessionId;

  useEffect(() => {
    onMessagesChange?.(runtime.messages);
  }, [onMessagesChange, runtime.messages]);

  const handleFileSelect = useCallback(
    (path: string) => {
      setWorkspacePath(path);
      // Selecting a file on mobile should bring the workspace tab to the
      // front so the user can see the file they just opened.
      setMobileView("workspace");
      onFileSelect?.(path);
    },
    [onFileSelect],
  );

  // Mobile toggle should only appear once there's something in the right
  // stack — otherwise it would just toggle to an empty pane. The workspace
  // panel always renders, but we still want the toggle even without a
  // browser panel because there can be files to browse.
  const showMobileToggle = Boolean(sessionId);

  return (
    <AgentChatAdapterProvider
      value={{
        adapter,
        sessionId,
        onFileSelect: handleFileSelect,
      }}
    >
      <TooltipProvider>
        <section
          data-testid="agent-chat-layout"
          data-mobile-view={mobileView}
          className={cn(
            // Phone: flex column, tab toggle on top, then either chat or
            // right stack visible based on `data-mobile-view`.
            "flex min-h-0 flex-1 flex-col overflow-hidden bg-background text-sm text-foreground",
            // md+: restore desktop two-pane layout.
            hasBrowserPanel
              ? "md:relative md:flex-row"
              : "md:flex-row",
          )}
          style={{ direction: "ltr", ...RIGHT_STACK_STYLE }}
        >
          {showMobileToggle && (
            <div className="md:hidden flex shrink-0 border-b border-line bg-card">
              <button
                type="button"
                onClick={() => setMobileView("chat")}
                aria-pressed={mobileView === "chat"}
                className={cn(
                  "flex-1 px-4 py-3 text-sm font-medium border-b-2 -mb-px transition-colors",
                  mobileView === "chat"
                    ? "border-primary text-foreground"
                    : "border-transparent text-subtle hover:text-foreground",
                )}
              >
                Chat
              </button>
              <button
                type="button"
                onClick={() => setMobileView("workspace")}
                aria-pressed={mobileView === "workspace"}
                className={cn(
                  "flex-1 px-4 py-3 text-sm font-medium border-b-2 -mb-px transition-colors",
                  mobileView === "workspace"
                    ? "border-primary text-foreground"
                    : "border-transparent text-subtle hover:text-foreground",
                )}
              >
                Workspace
              </button>
            </div>
          )}

          <div
            data-testid="chat-panel"
            data-mobile-view={mobileView}
            className={cn(
              // Phone: full width column, hidden when workspace tab active.
              "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden",
              showMobileToggle &&
                "data-[mobile-view=workspace]:hidden md:flex!",
              // md+: restore desktop positioning.
              hasBrowserPanel
                ? "md:absolute md:inset-y-0 md:left-0 md:right-(--right-stack-w,440px) md:flex"
                : "md:relative md:flex-1",
            )}
          >
            <ChatThread
              sessionId={sessionId}
              messages={runtime.messages}
              isRunning={runtime.isRunning}
              isLoadingHistory={runtime.isLoadingHistory}
              onSend={(content, images, attachments) =>
                runtime.send(content, images, attachments)
              }
              onStop={() => runtime.stop()}
              onRetry={runtime.retry}
              onFileSelect={handleFileSelect}
              disabled={effectiveDisabled}
              disabledReason={disabledReason}
              tokenUsage={runtime.tokenUsage}
              retryIndicator={runtime.retryIndicator}
              onComposerError={onComposerError}
            />
          </div>
          <div
            data-testid="right-stack"
            data-mobile-view={mobileView}
            className={cn(
              // Phone: full width column, hidden when chat tab active.
              "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden",
              showMobileToggle &&
                "data-[mobile-view=chat]:hidden md:flex!",
              // md+: restore desktop positioning. With browser panel the
              // right stack is absolutely positioned at the configured
              // width. Without it the stack lets the WorkspacePanel control
              // its own width (it has an internal user-resizable width).
              hasBrowserPanel
                ? "md:absolute md:inset-y-0 md:right-0 md:w-(--right-stack-w,440px) md:flex-none"
                : "md:relative md:shrink-0 md:flex-none md:w-auto",
            )}
          >
            {hasBrowserPanel && (
              <div
                data-testid="browser-panel"
                className="h-1/2 min-h-0 w-full overflow-hidden border-b border-line"
              >
                <BrowserPane
                  sessionId={sessionId}
                  state={browserState}
                  adapter={adapter}
                />
              </div>
            )}
            <div
              data-testid="workspace-panel-frame"
              className={
                hasBrowserPanel
                  ? "h-1/2 min-h-0 w-full overflow-hidden"
                  : "min-h-0 h-full"
              }
            >
              <WorkspacePanel
                adapter={adapter}
                sessionId={sessionId}
                selectedPath={workspacePath}
                onSelectedPathChange={setWorkspacePath}
                refreshSignal={runtime.workspaceRefreshKey}
                disabled={effectiveDisabled}
                fillParent={Boolean(hasBrowserPanel)}
              />
            </div>
          </div>
        </section>
      </TooltipProvider>
    </AgentChatAdapterProvider>
  );
}

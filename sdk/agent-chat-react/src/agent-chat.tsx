import { useCallback, useEffect, useState } from "react";
import { AgentChatAdapterProvider } from "./adapter-context";
import { BrowserPane } from "./components/browser/browser-pane";
import { ChatThread } from "./components/chat/chat-thread";
import { TooltipProvider } from "./components/ui/tooltip";
import { WorkspacePanel } from "./components/workspace/workspace-panel";
import { isScheduledRunSession } from "./lib/sessions";
import { useAgentChatRuntime } from "./runtime/use-agent-chat-runtime";
import type {
  AgentChatAdapter,
  AgentChatMessage,
} from "./types";

export interface AgentChatProps {
  adapter: AgentChatAdapter;
  agentId?: string;
  sessionId: string | null;
  onSessionChange?: (sessionId: string) => void;
  onFileSelect?: (path: string) => void;
  onMessagesChange?: (messages: AgentChatMessage[]) => void;
  disabled?: boolean;
}

export function AgentChat({
  adapter,
  agentId,
  sessionId,
  onSessionChange,
  onFileSelect,
  onMessagesChange,
  disabled,
}: AgentChatProps) {
  const [workspacePath, setWorkspacePath] = useState<string | null>(null);
  const [workspaceCollapsed, setWorkspaceCollapsed] = useState(false);
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
  const hasBrowserPanel = browserState !== null && sessionId;

  useEffect(() => {
    onMessagesChange?.(runtime.messages);
  }, [onMessagesChange, runtime.messages]);

  const handleFileSelect = useCallback(
    (path: string) => {
      setWorkspacePath(path);
      onFileSelect?.(path);
    },
    [onFileSelect],
  );

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
          className={
            hasBrowserPanel
              ? "relative min-h-0 flex-1 overflow-hidden bg-background text-sm text-foreground"
              : "flex min-h-0 flex-1 overflow-hidden bg-background text-sm text-foreground"
          }
          style={{ direction: "ltr" }}
        >
          <div
            data-testid="chat-panel"
            className={
              hasBrowserPanel
                ? "absolute inset-y-0 left-0 flex min-h-0 min-w-0 flex-col overflow-hidden"
                : "flex min-w-0 flex-1 flex-col overflow-hidden"
            }
            style={hasBrowserPanel ? { right: 440 } : undefined}
          >
            <ChatThread
              sessionId={sessionId}
              messages={runtime.messages}
              isRunning={runtime.isRunning}
              isLoadingHistory={runtime.isLoadingHistory}
              onSend={(content, images) => runtime.send(content, images)}
              onStop={() => runtime.stop()}
              onRetry={runtime.retry}
              onFileSelect={handleFileSelect}
              disabled={effectiveDisabled}
              disabledReason={disabledReason}
              tokenUsage={runtime.tokenUsage}
              retryIndicator={runtime.retryIndicator}
            />
          </div>
          <div
            data-testid="right-stack"
            className={
              hasBrowserPanel
                ? "absolute inset-y-0 flex min-h-0 flex-col overflow-hidden"
                : "flex min-h-0 shrink-0 flex-col"
            }
            style={hasBrowserPanel ? { right: 0, width: 440 } : undefined}
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
                  : "min-h-0"
              }
            >
              <WorkspacePanel
                adapter={adapter}
                sessionId={sessionId}
                selectedPath={workspacePath}
                onSelectedPathChange={setWorkspacePath}
                collapsed={workspaceCollapsed}
                onCollapsedChange={setWorkspaceCollapsed}
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

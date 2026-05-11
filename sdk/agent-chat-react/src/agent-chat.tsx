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
        <section className="flex flex-1 min-h-0 overflow-hidden bg-background text-sm text-foreground">
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
            <ChatThread
              sessionId={sessionId}
              messages={runtime.messages}
              isRunning={runtime.isRunning}
              isLoadingHistory={runtime.isLoadingHistory}
              onSend={(content, images) => void runtime.send(content, images)}
              onStop={() => void runtime.stop()}
              onRetry={runtime.retry}
              onFileSelect={handleFileSelect}
              disabled={effectiveDisabled}
              disabledReason={disabledReason}
              tokenUsage={runtime.tokenUsage}
              retryIndicator={runtime.retryIndicator}
            />
          </div>
          <div data-testid="right-stack" className="flex min-h-0 shrink-0 flex-col">
            {browserState !== null && sessionId && (
              <div
                data-testid="browser-panel"
                className="min-h-0 w-[400px] min-w-[300px] max-w-[900px] flex-1 overflow-hidden border-b border-line"
              >
                <BrowserPane
                  sessionId={sessionId}
                  state={browserState}
                  adapter={adapter}
                />
              </div>
            )}
            <WorkspacePanel
              adapter={adapter}
              sessionId={sessionId}
              selectedPath={workspacePath}
              onSelectedPathChange={setWorkspacePath}
              collapsed={workspaceCollapsed}
              onCollapsedChange={setWorkspaceCollapsed}
              refreshSignal={runtime.workspaceRefreshKey}
              disabled={effectiveDisabled}
            />
          </div>
        </section>
      </TooltipProvider>
    </AgentChatAdapterProvider>
  );
}

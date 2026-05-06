import { useCallback, useEffect, useState } from "react";
import { AgentChatAdapterProvider } from "./adapter-context";
import { ChatThread } from "./components/chat/chat-thread";
import { TooltipProvider } from "./components/ui/tooltip";
import { WorkspacePanel } from "./components/workspace/workspace-panel";
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
  const runtime = useAgentChatRuntime({
    adapter,
    agentId,
    sessionId,
    onSessionChange,
  });

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
        <section className="flex h-full min-h-0 bg-card text-sm text-foreground">
          <div className="flex min-w-0 flex-1 flex-col">
            <ChatThread
              sessionId={sessionId}
              messages={runtime.messages}
              isRunning={runtime.isRunning}
              onSend={(content) => void runtime.send(content)}
              onStop={() => void runtime.stop()}
              onRetry={runtime.retry}
              onFileSelect={handleFileSelect}
              disabled={disabled}
              tokenUsage={runtime.tokenUsage}
              retryIndicator={runtime.retryIndicator}
            />
          </div>
          <WorkspacePanel
            adapter={adapter}
            sessionId={sessionId}
            selectedPath={workspacePath}
            onSelectedPathChange={setWorkspacePath}
            disabled={disabled}
          />
        </section>
      </TooltipProvider>
    </AgentChatAdapterProvider>
  );
}

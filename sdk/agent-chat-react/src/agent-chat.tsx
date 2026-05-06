import { useEffect } from "react";
import { AgentChatAdapterProvider } from "./adapter-context";
import { ChatThread } from "./components/chat/chat-thread";
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
  const runtime = useAgentChatRuntime({
    adapter,
    agentId,
    sessionId,
    onSessionChange,
  });

  useEffect(() => {
    onMessagesChange?.(runtime.messages);
  }, [onMessagesChange, runtime.messages]);

  return (
    <AgentChatAdapterProvider
      value={{
        adapter,
        sessionId,
        onFileSelect,
      }}
    >
      <section className="flex h-full min-h-0 flex-col bg-card text-sm text-foreground">
        <ChatThread
          sessionId={sessionId}
          messages={runtime.messages}
          isRunning={runtime.isRunning}
          onSend={(content) => void runtime.send(content)}
          onStop={() => void runtime.stop()}
          onRetry={runtime.retry}
          onFileSelect={onFileSelect}
          disabled={disabled}
          tokenUsage={runtime.tokenUsage}
          retryIndicator={runtime.retryIndicator}
        />
      </section>
    </AgentChatAdapterProvider>
  );
}

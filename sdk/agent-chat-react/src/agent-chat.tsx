import { FormEvent, useMemo, useState } from "react";
import { AgentChatAdapterProvider } from "./adapter-context";
import { useAgentChatRuntime } from "./runtime/use-agent-chat-runtime";
import type {
  AgentChatAdapter,
  AgentChatMessage,
  AgentChatRetryIndicator,
  AgentChatTokenUsage,
  AgentChatToolCallInfo,
} from "./types";

export interface AgentChatProps {
  adapter: AgentChatAdapter;
  agentId?: string;
  sessionId: string | null;
  onSessionChange?: (sessionId: string) => void;
  onFileSelect?: (path: string) => void;
  disabled?: boolean;
}

export function AgentChat(_props: AgentChatProps) {
  const runtime = useAgentChatRuntime({
    adapter: _props.adapter,
    agentId: _props.agentId,
    sessionId: _props.sessionId,
    onSessionChange: _props.onSessionChange,
  });

  return (
    <AgentChatAdapterProvider
      value={{
        adapter: _props.adapter,
        sessionId: _props.sessionId,
        onFileSelect: _props.onFileSelect,
      }}
    >
      <section className="flex h-full min-h-0 flex-col bg-card text-sm text-foreground">
        <MessageList
          messages={runtime.messages}
          retryIndicator={runtime.retryIndicator}
        />
        <Composer
          disabled={_props.disabled}
          isRunning={runtime.isRunning}
          tokenUsage={runtime.tokenUsage}
          onSend={runtime.send}
          onStop={runtime.stop}
        />
      </section>
    </AgentChatAdapterProvider>
  );
}

function MessageList({
  messages,
  retryIndicator,
}: {
  messages: AgentChatMessage[];
  retryIndicator: AgentChatRetryIndicator | null;
}) {
  const renderedMessages = useMemo(
    () => messages.map((message) => (
      <MessageBubble key={message.id} message={message} />
    )),
    [messages],
  );

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-6">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-4">
        {messages.length === 0 ? (
          <div className="flex min-h-64 items-center justify-center text-center text-muted-foreground">
            <div>
              <div className="text-base font-semibold text-foreground">
                Hello there!
              </div>
              <div className="mt-1">How can I help you today?</div>
            </div>
          </div>
        ) : (
          renderedMessages
        )}
        {retryIndicator && <RetryNotice indicator={retryIndicator} />}
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: AgentChatMessage }) {
  if (message.role === "system") {
    return <SystemMessage message={message} />;
  }

  const isUser = message.role === "user";
  return (
    <article
      className={
        isUser
          ? "ml-auto max-w-[80%] rounded-md bg-primary px-3 py-2 text-primary-foreground"
          : "mr-auto w-full max-w-full text-foreground"
      }
    >
      {message.reasoning && (
        <details className="mb-2 rounded-md border border-border bg-muted/30 p-3 text-muted-foreground">
          <summary className="cursor-pointer text-xs font-semibold text-foreground">
            Reasoning
          </summary>
          <div className="mt-2 whitespace-pre-wrap">{message.reasoning}</div>
        </details>
      )}
      {message.content && (
        <div className="whitespace-pre-wrap leading-relaxed">
          {message.content}
        </div>
      )}
      {message.toolCalls && message.toolCalls.length > 0 && (
        <div className="mt-3 flex flex-col gap-2">
          {message.toolCalls.map((toolCall) => (
            <ToolCallRow key={toolCall.id} toolCall={toolCall} />
          ))}
        </div>
      )}
      {message.errorInfo && (
        <div className="mt-3 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-destructive">
          <div className="font-semibold">{message.errorInfo.title}</div>
          {message.errorInfo.detail && (
            <pre className="mt-2 whitespace-pre-wrap text-xs">
              {message.errorInfo.detail}
            </pre>
          )}
        </div>
      )}
    </article>
  );
}

function SystemMessage({ message }: { message: AgentChatMessage }) {
  if (message.systemKind === "skill_invoked") {
    return (
      <div className="text-xs text-muted-foreground">
        Skill invoked:{" "}
        <span className="font-semibold text-foreground">{message.content}</span>
      </div>
    );
  }

  if (message.systemKind === "artifact") {
    return (
      <div className="rounded-md border border-border bg-background p-3">
        <div className="text-xs font-semibold uppercase text-muted-foreground">
          Artifact
        </div>
        <div className="mt-1 font-medium">{message.content}</div>
        <div className="mt-1 text-xs text-muted-foreground">
          {String(message.systemMeta?.kind ?? "artifact")} v
          {String(message.systemMeta?.version ?? "1")}
        </div>
      </div>
    );
  }

  if (message.errorInfo) {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-destructive">
        <div className="font-semibold">{message.errorInfo.title}</div>
        {message.errorInfo.detail && (
          <pre className="mt-2 whitespace-pre-wrap text-xs">
            {message.errorInfo.detail}
          </pre>
        )}
      </div>
    );
  }

  return null;
}

function ToolCallRow({ toolCall }: { toolCall: AgentChatToolCallInfo }) {
  const statusClass = {
    running: "border-amber-500/40 bg-amber-500/5",
    complete: "border-emerald-500/40 bg-emerald-500/5",
    error: "border-destructive/40 bg-destructive/5",
  }[toolCall.status];

  return (
    <details className={`rounded-md border p-3 ${statusClass}`}>
      <summary className="cursor-pointer text-sm font-semibold">
        {toolCall.toolName}{" "}
        <span className="font-normal text-muted-foreground">
          {toolCall.status}
        </span>
      </summary>
      {toolCall.args && (
        <pre className="mt-2 overflow-x-auto whitespace-pre-wrap text-xs text-muted-foreground">
          {toolCall.args}
        </pre>
      )}
      {toolCall.result && (
        <pre className="mt-2 overflow-x-auto whitespace-pre-wrap text-xs">
          {toolCall.result}
        </pre>
      )}
      {toolCall.clarifyAnswers && (
        <div className="mt-2 space-y-1 text-xs">
          {toolCall.clarifyAnswers.map((answer) => (
            <div key={`${answer.question}:${answer.answer}`}>
              <span className="font-semibold">{answer.question}</span>:{" "}
              {answer.answer}
            </div>
          ))}
        </div>
      )}
    </details>
  );
}

function RetryNotice({ indicator }: { indicator: AgentChatRetryIndicator }) {
  return (
    <div
      role="status"
      className="rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-700"
    >
      <div className="font-semibold">
        {indicator.title}{" "}
        <span className="font-normal text-muted-foreground">
          attempt {indicator.attempt}
        </span>
      </div>
      {indicator.detail && (
        <pre className="mt-1 whitespace-pre-wrap text-muted-foreground">
          {indicator.detail}
        </pre>
      )}
    </div>
  );
}

function Composer({
  disabled,
  isRunning,
  tokenUsage,
  onSend,
  onStop,
}: {
  disabled?: boolean;
  isRunning: boolean;
  tokenUsage: AgentChatTokenUsage;
  onSend: (content: string) => Promise<void>;
  onStop: () => Promise<void>;
}) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const canSend = value.trim().length > 0 && !disabled && !isRunning;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSend) return;
    const content = value.trim();
    setValue("");
    setError(null);
    try {
      await onSend(content);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to send message");
    }
  }

  return (
    <form
      onSubmit={(event) => void handleSubmit(event)}
      className="mx-auto w-full max-w-4xl border-t border-border px-4 py-3"
    >
      {error && (
        <div className="mb-2 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      )}
      <div className="flex items-end gap-2">
        <textarea
          value={value}
          onChange={(event) => setValue(event.target.value)}
          disabled={disabled}
          rows={2}
          className="min-h-12 flex-1 resize-none rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus:border-ring"
          placeholder={disabled ? "Chat disabled" : "Message the agent"}
        />
        {isRunning ? (
          <button
            type="button"
            onClick={() => void onStop()}
            className="rounded-md border border-border px-3 py-2 text-sm font-medium"
          >
            Stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={!canSend}
            className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            Send
          </button>
        )}
      </div>
      {tokenUsage.totalTokens > 0 && (
        <div className="mt-2 text-xs text-muted-foreground">
          {tokenUsage.totalTokens.toLocaleString()} tokens
          {tokenUsage.model ? ` - ${tokenUsage.model}` : ""}
        </div>
      )}
    </form>
  );
}

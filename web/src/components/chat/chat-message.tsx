// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { memo } from "react";
import { Streamdown } from "streamdown";
import { cn } from "@/lib/utils";
import { ToolCallBlock } from "./tool-call-block";
import type { ChatMessage as ChatMessageType } from "@/hooks/use-session-runtime";

interface ChatMessageProps {
  message: ChatMessageType;
  isLast: boolean;
}

export const ChatMessage = memo(function ChatMessage({
  message,
  isLast,
}: ChatMessageProps) {
  if (message.role === "user") {
    return <UserMessage content={message.content} />;
  }
  return (
    <AssistantMessage
      message={message}
      isLast={isLast}
    />
  );
});

function UserMessage({ content }: { content: string }) {
  return (
    <div className="flex justify-end py-2">
      <div className="max-w-[85%] rounded-2xl bg-muted px-4 py-2.5 text-sm">
        {content}
      </div>
    </div>
  );
}

function AssistantMessage({
  message,
  isLast,
}: {
  message: ChatMessageType;
  isLast: boolean;
}) {
  const hasContent = !!message.content;
  const hasReasoning = !!message.reasoning;
  const hasToolCalls = !!(message.toolCalls && message.toolCalls.length > 0);
  const isStreaming = message.status === "streaming" && isLast;

  return (
    <div className="py-2">
      {hasReasoning && (
        <div className="text-sm text-muted-foreground mb-1">
          <Streamdown>{message.reasoning!}</Streamdown>
        </div>
      )}

      {hasToolCalls && (
        <div className="my-1 border-l-2 border-border pl-1">
          {message.toolCalls!.map((tc) => (
            <ToolCallBlock key={tc.id} tc={tc} />
          ))}
        </div>
      )}

      {hasContent && (
        <div className={cn("text-sm", hasToolCalls && "mt-2")}>
          <Streamdown>{message.content}</Streamdown>
        </div>
      )}

      {isStreaming && !hasContent && !hasToolCalls && !hasReasoning && (
        <div className="flex items-center gap-1.5 py-1 text-muted-foreground">
          <span className="inline-block size-1.5 animate-pulse rounded-full bg-primary" />
          <span className="text-xs">Thinking...</span>
        </div>
      )}
    </div>
  );
}

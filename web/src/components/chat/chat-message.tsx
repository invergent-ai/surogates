// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { memo } from "react";
import {
  Message,
  MessageContent,
  MessageResponse,
} from "@/components/ai-elements/message";
import {
  Reasoning,
  ReasoningContent,
  ReasoningTrigger,
} from "@/components/ai-elements/reasoning";
import { ToolCallBlock } from "./tool-call-block";
import type { ChatMessage as ChatMessageType } from "@/hooks/use-session-runtime";

interface ChatMessageProps {
  message: ChatMessageType;
  isLast: boolean;
  onFileSelect?: (path: string) => void;
}

export const ChatMessage = memo(function ChatMessage({
  message,
  isLast,
  onFileSelect,
}: ChatMessageProps) {
  if (message.role === "user") {
    return (
      <Message from="user">
        <MessageContent>{message.content}</MessageContent>
      </Message>
    );
  }

  return <AssistantMessage message={message} isLast={isLast} onFileSelect={onFileSelect} />;
});

function AssistantMessage({
  message,
  isLast,
  onFileSelect,
}: {
  message: ChatMessageType;
  isLast: boolean;
  onFileSelect?: (path: string) => void;
}) {
  const hasReasoning = !!message.reasoning;
  const hasToolCalls = !!(message.toolCalls && message.toolCalls.length > 0);
  const isStreaming = message.status === "streaming" && isLast;
  // Guard: if content is identical to reasoning, skip it (dedup).
  const hasContent = !!(
    message.content &&
    message.content !== message.reasoning
  );

  return (
    <Message from="assistant">
      <MessageContent>
        {hasReasoning && (
          <Reasoning isStreaming={isStreaming && !hasContent && !hasToolCalls}>
            <ReasoningTrigger />
            <ReasoningContent>{message.reasoning!}</ReasoningContent>
          </Reasoning>
        )}

        {hasToolCalls && (
          <div className="w-full flex flex-col gap-8">
            {message.toolCalls!.map((tc) => (
              <ToolCallBlock key={tc.id} tc={tc} onFileSelect={onFileSelect} />
            ))}
          </div>
        )}

        {hasContent && (
          <MessageResponse>{message.content}</MessageResponse>
        )}

        {isStreaming && !hasContent && !hasToolCalls && !hasReasoning && (
          <div className="flex items-center gap-1.5 py-1 text-muted-foreground">
            <span className="inline-block size-1.5 animate-pulse rounded-full bg-primary" />
            <span className="text-xs">Thinking...</span>
          </div>
        )}
      </MessageContent>
    </Message>
  );
}

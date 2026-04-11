// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Custom chat thread — uses ai-elements Conversation + Message
// with a compact, Claude Code-inspired layout.
//
import { useMemo } from "react";
import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
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
import {
  Timeline,
  TimelineContent,
  TimelineHeader,
  TimelineIndicator,
  TimelineItem,
  TimelineSeparator,
} from "@/components/reui/timeline";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { ToolCallBlock, statusColorClass } from "./tool-call-block";
import { ChatMessage } from "./chat-message";
import { ChatComposer } from "./chat-composer";
import { cn } from "@/lib/utils";
import { MessageSquareIcon } from "lucide-react";
import type { ChatMessage as ChatMessageType, ToolCallInfo } from "@/hooks/use-session-runtime";

interface ChatThreadProps {
  messages: ChatMessageType[];
  isRunning: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  onFileSelect?: (path: string) => void;
  disabled?: boolean;
}

// ── Timeline item types ──────────────────────────────────────────────

type TimelineEntry =
  | { kind: "reasoning"; key: string; reasoning: string; isStreaming: boolean }
  | { kind: "tool"; key: string; tc: ToolCallInfo }
  | { kind: "text"; key: string; content: string }
  | { kind: "thinking"; key: string };

/**
 * Flatten an assistant message into a list of timeline entries
 * (reasoning, tool calls, text content).
 */
function messageToEntries(
  msg: ChatMessageType,
  isLast: boolean,
): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  const hasToolCalls = !!(msg.toolCalls && msg.toolCalls.length > 0);
  const hasContent = !!(msg.content && msg.content !== msg.reasoning);
  const isStreaming = msg.status === "streaming" && isLast;

  if (msg.reasoning) {
    entries.push({
      kind: "reasoning",
      key: `${msg.id}-reasoning`,
      reasoning: msg.reasoning,
      isStreaming: isStreaming && !hasContent && !hasToolCalls,
    });
  }

  if (hasToolCalls) {
    for (const tc of msg.toolCalls!) {
      entries.push({ kind: "tool", key: tc.id, tc });
    }
  }

  if (hasContent) {
    entries.push({ kind: "text", key: `${msg.id}-text`, content: msg.content });
  }

  if (isStreaming && !hasContent && !hasToolCalls && !msg.reasoning) {
    entries.push({ kind: "thinking", key: `${msg.id}-thinking` });
  }

  return entries;
}

/** A run of consecutive messages grouped by role. */
interface MessageGroup {
  role: "user" | "assistant";
  messages: ChatMessageType[];
  lastGlobalIndex: number;
}

function groupMessages(messages: ChatMessageType[]): MessageGroup[] {
  const groups: MessageGroup[] = [];
  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];
    const prev = groups[groups.length - 1];
    if (prev && prev.role === msg.role && msg.role === "assistant") {
      prev.messages.push(msg);
      prev.lastGlobalIndex = i;
    } else {
      groups.push({ role: msg.role, messages: [msg], lastGlobalIndex: i });
    }
  }
  return groups;
}

// ── Timeline entry renderer ──────────────────────────────────────────

function TimelineEntryItem({
  entry,
  step,
  onFileSelect,
}: {
  entry: TimelineEntry;
  step: number;
  onFileSelect?: (path: string) => void;
}) {
  if (entry.kind === "reasoning") {
    return (
      <TimelineItem step={step}>
        <TimelineHeader>
          <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
          <TimelineIndicator className="size-2" />
        </TimelineHeader>
        <TimelineContent>
          <Reasoning isStreaming={entry.isStreaming}>
            <ReasoningTrigger />
            <ReasoningContent>{entry.reasoning}</ReasoningContent>
          </Reasoning>
        </TimelineContent>
      </TimelineItem>
    );
  }

  if (entry.kind === "tool") {
    return (
      <TimelineItem step={step}>
        <TimelineHeader>
          <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
          <TimelineIndicator
            className={cn("size-2 border-none", statusColorClass(entry.tc.status))}
          />
        </TimelineHeader>
        <TimelineContent>
          <ToolCallBlock tc={entry.tc} onFileSelect={onFileSelect} />
        </TimelineContent>
      </TimelineItem>
    );
  }

  if (entry.kind === "text") {
    return (
      <TimelineItem step={step}>
        <TimelineHeader>
          <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
          <TimelineIndicator className="size-2 border-none bg-foreground/40" />
        </TimelineHeader>
        <TimelineContent>
          <MessageResponse>{entry.content}</MessageResponse>
        </TimelineContent>
      </TimelineItem>
    );
  }

  // kind === "thinking"
  return (
    <TimelineItem step={step}>
      <TimelineHeader>
        <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
        <TimelineIndicator className="size-2 border-none bg-primary animate-pulse" />
      </TimelineHeader>
      <TimelineContent>
        <Shimmer duration={1.5} className="text-sm">Working on it...</Shimmer>
      </TimelineContent>
    </TimelineItem>
  );
}

// ── Assistant message group (single Timeline) ────────────────────────

function AssistantGroup({
  messages,
  lastGlobalIndex,
  totalMessages,
  onFileSelect,
}: {
  messages: ChatMessageType[];
  lastGlobalIndex: number;
  totalMessages: number;
  onFileSelect?: (path: string) => void;
}) {
  const entries: TimelineEntry[] = [];
  for (const msg of messages) {
    const isLast = messages.indexOf(msg) === messages.length - 1
      && lastGlobalIndex === totalMessages - 1;
    entries.push(...messageToEntries(msg, isLast));
  }

  return (
    <Message from="assistant">
      <MessageContent>
        <Timeline defaultValue={999} className="gap-0">
          {entries.map((entry, i) => (
            <TimelineEntryItem
              key={entry.key}
              entry={entry}
              step={i + 1}
              onFileSelect={onFileSelect}
            />
          ))}
        </Timeline>
      </MessageContent>
    </Message>
  );
}

// ── Main thread ──────────────────────────────────────────────────────

export function ChatThread({
  messages,
  isRunning,
  onSend,
  onStop,
  onFileSelect,
  disabled = false,
}: ChatThreadProps) {
  const groups = useMemo(() => groupMessages(messages), [messages]);

  return (
    <div className="flex h-full flex-col overflow-hidden bg-card text-sm">
      <Conversation className="relative flex-1 min-h-0">
        <ConversationContent className="mx-auto max-w-4xl">
          {messages.length === 0 && !disabled ? (
            <ConversationEmptyState
              icon={<MessageSquareIcon className="size-6" />}
              title="Hello there!"
              description="How can I help you today?"
            />
          ) : (
            <>
              {groups.map((group) => {
                if (group.role === "user") {
                  const msg = group.messages[0];
                  return (
                    <ChatMessage
                      key={msg.id}
                      message={msg}
                      isLast={group.lastGlobalIndex === messages.length - 1}
                      onFileSelect={onFileSelect}
                    />
                  );
                }

                return (
                  <AssistantGroup
                    key={group.messages[0].id}
                    messages={group.messages}
                    lastGlobalIndex={group.lastGlobalIndex}
                    totalMessages={messages.length}
                    onFileSelect={onFileSelect}
                  />
                );
              })}
              {isRunning && messages.length > 0 && messages[messages.length - 1].role === "user" && (
                <Message from="assistant">
                  <MessageContent>
                    <Shimmer duration={1.5} className="text-sm">Working on it...</Shimmer>
                  </MessageContent>
                </Message>
              )}
            </>
          )}
        </ConversationContent>
        <ConversationScrollButton />
      </Conversation>

      <div className="mx-auto w-full max-w-3xl px-4 pb-4 pt-2">
        <ChatComposer
          onSend={onSend}
          onStop={onStop}
          isRunning={isRunning}
          disabled={disabled}
        />
      </div>
    </div>
  );
}

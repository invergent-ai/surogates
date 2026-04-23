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
import { ToolCallBlock } from "./tool-call-block";
import { statusColorClass, effectiveStatus } from "./tools/shared";
import { ChatMessage } from "./chat-message";
import { ChatComposer } from "./chat-composer";
import { ArtifactBlock } from "./artifacts/artifact-block";
import { ErrorMessage } from "./error-message";
import { cn } from "@/lib/utils";
import { AlertTriangle, ChevronDown, ChevronRight, MessageSquareIcon } from "lucide-react";
import { useState } from "react";
import type {
  ChatMessage as ChatMessageType,
  RetryIndicator,
  ToolCallInfo,
  TokenUsage,
} from "@/hooks/use-session-runtime";
import type { ArtifactKind } from "@/types/session";

interface ChatThreadProps {
  sessionId: string | null;
  messages: ChatMessageType[];
  isRunning: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  onFileSelect?: (path: string) => void;
  disabled?: boolean;
  tokenUsage?: TokenUsage;
  // Transient indicator shown during provider retries.  Cleared by the
  // hook on the next successful llm.request/response or on session.fail.
  retryIndicator?: RetryIndicator | null;
  // User-initiated retry of a failed or paused session.  Called from the
  // Retry button on standalone error bubbles.
  onRetry?: () => Promise<void>;
}

// ── Timeline item types ──────────────────────────────────────────────

type TimelineEntry =
  | { kind: "reasoning"; key: string; reasoning: string; isStreaming: boolean }
  | { kind: "tool"; key: string; tc: ToolCallInfo }
  | { kind: "text"; key: string; content: string }
  | { kind: "thinking"; key: string }
  | { kind: "skill_invoked"; key: string; skill: string; stagedAt: string | null }
  | {
      kind: "artifact";
      key: string;
      artifactId: string;
      name: string;
      artifactKind: ArtifactKind;
      version: number;
    };

/**
 * Flatten an assistant message into a list of timeline entries
 * (reasoning, tool calls, text content).
 */
function messageToEntries(
  msg: ChatMessageType,
  isLast: boolean,
): TimelineEntry[] {
  // System markers (skill.invoked, ...) become their own timeline entry --
  // a single row with a green dot + label, threaded into the assistant's
  // vertical timeline above its first reasoning/tool-call entry.
  if (msg.role === "system") {
    if (msg.systemKind === "skill_invoked") {
      return [{
        kind: "skill_invoked",
        key: msg.id,
        skill: (msg.systemMeta?.skill as string) ?? msg.content,
        stagedAt: (msg.systemMeta?.staged_at as string | null | undefined) ?? null,
      }];
    }
    if (msg.systemKind === "artifact") {
      const { artifactId, name, kind, version } = unpackArtifactMeta(
        msg.systemMeta, msg.content,
      );
      return [{
        kind: "artifact",
        key: msg.id,
        artifactId,
        name,
        artifactKind: kind,
        version,
      }];
    }
    return [];
  }

  const entries: TimelineEntry[] = [];
  const hasToolCalls = !!(msg.toolCalls && msg.toolCalls.length > 0);
  const hasContent = !!(msg.content && msg.content !== msg.reasoning);
  const isStreaming = msg.status === "streaming" && isLast;

  // When tool calls are present, the content text is just a preamble
  // ("I'll run both tasks in parallel...") — fold it into reasoning
  // instead of showing it as a separate text block.
  const effectiveReasoning = hasToolCalls && hasContent
    ? (msg.reasoning ? msg.reasoning + "\n" + msg.content : msg.content)
    : msg.reasoning;
  const effectiveHasContent = hasContent && !hasToolCalls;

  if (effectiveReasoning) {
    entries.push({
      kind: "reasoning",
      key: `${msg.id}-reasoning`,
      reasoning: effectiveReasoning,
      isStreaming: isStreaming && !effectiveHasContent && !hasToolCalls,
    });
  }

  if (hasToolCalls) {
    for (const tc of msg.toolCalls!) {
      entries.push({ kind: "tool", key: tc.id, tc });
    }
  }

  if (effectiveHasContent) {
    entries.push({ kind: "text", key: `${msg.id}-text`, content: msg.content });
  }

  // Show "Working on it..." shimmer whenever the turn is active but
  // nothing visible is progressing:
  //   - initial thinking before the first reasoning/tool/content arrives
  //   - post-reasoning gap (reasoning has landed but the next tool or
  //     content hasn't -- common between llm.response and tool.call, or
  //     while the LLM is still composing the next iteration)
  //   - between tool rounds once every tool call has completed
  // A running tool call already shows its own shimmer, so skip then.
  const hasRunningTool = hasToolCalls && msg.toolCalls!.some(
    (tc) => tc.status === "running",
  );
  if (isStreaming && !effectiveHasContent && !hasRunningTool) {
    entries.push({ kind: "thinking", key: `${msg.id}-thinking` });
  }

  return entries;
}

/** A run of consecutive messages grouped by role.
 *
 * System markers (``skill.invoked``) are folded into the *following*
 * assistant group as leading messages, so they render as the first item
 * of the assistant's vertical timeline alongside its reasoning and tool
 * calls -- not as a floating row outside the timeline.  Trailing system
 * markers (no assistant turn yet) get their own ``system`` group.
 */
interface MessageGroup {
  role: "user" | "assistant" | "system";
  messages: ChatMessageType[];
  lastGlobalIndex: number;
}

function groupMessages(messages: ChatMessageType[]): MessageGroup[] {
  const groups: MessageGroup[] = [];
  let pendingSystem: { msg: ChatMessageType; index: number }[] = [];

  const drainPendingAsOrphans = () => {
    for (const { msg, index } of pendingSystem) {
      groups.push({ role: "system", messages: [msg], lastGlobalIndex: index });
    }
    pendingSystem = [];
  };

  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i];

    if (msg.role === "system") {
      pendingSystem.push({ msg, index: i });
      continue;
    }

    if (msg.role === "assistant") {
      const prev = groups[groups.length - 1];
      if (prev && prev.role === "assistant" && pendingSystem.length === 0) {
        prev.messages.push(msg);
        prev.lastGlobalIndex = i;
      } else {
        const folded = pendingSystem.map((p) => p.msg);
        pendingSystem = [];
        groups.push({
          role: "assistant",
          messages: [...folded, msg],
          lastGlobalIndex: i,
        });
      }
      continue;
    }

    // user — drain any orphan system markers (no assistant followed),
    // then push the user turn as its own group.
    drainPendingAsOrphans();
    groups.push({ role: "user", messages: [msg], lastGlobalIndex: i });
  }

  drainPendingAsOrphans();
  return groups;
}

function unpackArtifactMeta(
  systemMeta: Record<string, unknown> | undefined,
  fallbackName: string,
): { artifactId: string; name: string; kind: ArtifactKind; version: number } {
  const meta = systemMeta ?? {};
  return {
    artifactId: (meta.artifact_id as string) ?? "",
    name: (meta.name as string) ?? fallbackName,
    kind: (meta.kind as ArtifactKind) ?? "markdown",
    version: (meta.version as number) ?? 1,
  };
}

// ── Orphan system marker (no following assistant yet) ───────────────
//
// Rendered only when a system event arrives but the LLM has not yet
// produced an assistant turn to fold it into.

function OrphanSystemMarker({
  message,
  sessionId,
  onRetry,
}: {
  message: ChatMessageType;
  sessionId: string | null;
  onRetry?: () => Promise<void>;
}) {
  if (message.systemKind === "skill_invoked") {
    const skill = (message.systemMeta?.skill as string) ?? message.content;
    return (
      <div className="my-2 flex items-center gap-2 px-4 text-xs text-muted-foreground ">
        <span className="size-2 rounded-full bg-emerald-500" />
        <span>
          <span className="font-semibold text-foreground">Skill</span>
          <span className="text-muted-foreground truncate">{skill}</span>
        </span>
      </div>
    );
  }

  if (message.systemKind === "artifact" && sessionId) {
    const unpacked = unpackArtifactMeta(message.systemMeta, message.content);
    return (
      <ArtifactBlock
        sessionId={sessionId}
        artifactId={unpacked.artifactId}
        name={unpacked.name}
        kind={unpacked.kind}
        version={unpacked.version}
      />
    );
  }

  if (message.systemKind === "error" && message.errorInfo) {
    return (
      <div className="mx-auto my-2 w-full max-w-4xl px-4">
        <ErrorMessage errorInfo={message.errorInfo} onRetry={onRetry} />
      </div>
    );
  }

  return null;
}

// ── Timeline entry renderer ──────────────────────────────────────────

function TimelineEntryItem({
  entry,
  step,
  sessionId,
  onFileSelect,
}: {
  entry: TimelineEntry;
  step: number;
  sessionId: string | null;
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
            className={cn("size-2 border-none", statusColorClass(effectiveStatus(entry.tc)))}
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

  if (entry.kind === "skill_invoked") {
    return (
      <TimelineItem step={step}>
        <TimelineHeader>
          <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
          <TimelineIndicator className="size-2 border-none bg-emerald-500" />
        </TimelineHeader>
        <TimelineContent>
          <div className="flex items-center gap-1.5 py-1 text-sm ">
            <span className="font-semibold text-foreground">Skill</span>
            <span className="text-muted-foreground truncate">
              {entry.skill}
            </span>
            {entry.stagedAt && (
              <span className="text-xs text-muted-foreground/70 ">
                staged at {entry.stagedAt}
              </span>
            )}
          </div>
        </TimelineContent>
      </TimelineItem>
    );
  }

  if (entry.kind === "artifact") {
    return (
      <TimelineItem step={step}>
        <TimelineHeader>
          <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
          <TimelineIndicator className="size-2 border-none bg-sky-500" />
        </TimelineHeader>
        <TimelineContent>
          {sessionId ? (
            <ArtifactBlock
              sessionId={sessionId}
              artifactId={entry.artifactId}
              name={entry.name}
              kind={entry.artifactKind}
              version={entry.version}
            />
          ) : null}
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
        <Shimmer duration={5} className="text-sm text-foreground">Working on it...</Shimmer>
      </TimelineContent>
    </TimelineItem>
  );
}

// ── Assistant message group (single Timeline) ────────────────────────

function AssistantGroup({
  messages,
  lastGlobalIndex,
  totalMessages,
  sessionId,
  onFileSelect,
  onRetry,
}: {
  messages: ChatMessageType[];
  lastGlobalIndex: number;
  totalMessages: number;
  sessionId: string | null;
  onFileSelect?: (path: string) => void;
  onRetry?: () => Promise<void>;
}) {
  const entries: TimelineEntry[] = [];
  for (let i = 0; i < messages.length; i++) {
    const isLast = i === messages.length - 1
      && lastGlobalIndex === totalMessages - 1;
    entries.push(...messageToEntries(messages[i], isLast));
  }

  // Surface the classifier's error info inline below the timeline when
  // the last assistant message in the group ended in error.
  const tail = messages[messages.length - 1];
  const showErrorInfo =
    tail && tail.role === "assistant"
      && tail.status === "error"
      && !!tail.errorInfo;

  return (
    <Message from="assistant">
      <MessageContent>
        <Timeline defaultValue={999} className="gap-0">
          {entries.map((entry, i) => (
            <TimelineEntryItem
              key={entry.key}
              entry={entry}
              step={i + 1}
              sessionId={sessionId}
              onFileSelect={onFileSelect}
            />
          ))}
        </Timeline>
        {showErrorInfo && (
          <div className="mt-3">
            <ErrorMessage errorInfo={tail.errorInfo!} onRetry={onRetry} />
          </div>
        )}
      </MessageContent>
    </Message>
  );
}

// Transient banner shown above the composer while the orchestrator is
// retrying after a provider error.  Collapsible for the raw detail.
function RetryBanner({ indicator }: { indicator: RetryIndicator }) {
  const [open, setOpen] = useState(false);
  return (
    <div
      role="status"
      className="flex flex-col gap-1 border-l-2 border-amber-500 bg-amber-500/5 px-3 py-2 text-xs"
    >
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="flex items-center gap-2 text-left text-amber-600 hover:text-amber-700"
        aria-expanded={open}
      >
        <AlertTriangle className="size-3 shrink-0" />
        <span className="flex-1 truncate font-medium">{indicator.title}</span>
        <span className="text-[10px] text-muted-foreground">
          attempt {indicator.attempt}
        </span>
        {indicator.detail && (
          open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />
        )}
      </button>
      {open && indicator.detail && (
        <pre className="mt-1 overflow-x-auto rounded-none bg-background p-2 font-mono text-[11px] whitespace-pre-wrap wrap-break-word text-muted-foreground">
          {indicator.detail}
        </pre>
      )}
    </div>
  );
}

// ── Main thread ──────────────────────────────────────────────────────

export function ChatThread({
  sessionId,
  messages,
  isRunning,
  onSend,
  onStop,
  onFileSelect,
  disabled = false,
  tokenUsage,
  retryIndicator,
  onRetry,
}: ChatThreadProps) {
  const groups = useMemo(() => groupMessages(messages), [messages]);

  // Retry is only actionable for the most recent unresolved failure.
  // An error bubble (standalone system or inline assistant) is "active"
  // only when it is the tail of the message list — anything after it
  // (a new user message, a later successful turn, another failure)
  // means the server-side state has moved on and clicking the older
  // button would 409.  We resolve the active failure's id once here
  // and pass onRetry only to the matching render site.
  const activeFailureId = useMemo<string | null>(() => {
    if (messages.length === 0) return null;
    const tail = messages[messages.length - 1];
    if (tail.role === "system" && tail.systemKind === "error" && tail.errorInfo) {
      return tail.id;
    }
    if (tail.role === "assistant" && tail.status === "error" && tail.errorInfo) {
      return tail.id;
    }
    return null;
  }, [messages]);

  return (
    <div className="flex h-full flex-col overflow-hidden bg-card text-sm">
      <Conversation className="relative flex-1 min-h-0">
        <ConversationContent className="mx-auto">
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

                if (group.role === "system") {
                  const msg = group.messages[0];
                  const groupRetry =
                    msg.id === activeFailureId ? onRetry : undefined;
                  return (
                    <OrphanSystemMarker
                      key={msg.id}
                      message={msg}
                      sessionId={sessionId}
                      onRetry={groupRetry}
                    />
                  );
                }

                // Only the assistant group whose tail message is the
                // active failure gets the onRetry callback; earlier
                // failed turns render read-only.
                const groupTail = group.messages[group.messages.length - 1];
                const groupRetry =
                  groupTail.id === activeFailureId ? onRetry : undefined;
                return (
                  <AssistantGroup
                    key={group.messages[0].id}
                    messages={group.messages}
                    lastGlobalIndex={group.lastGlobalIndex}
                    totalMessages={messages.length}
                    sessionId={sessionId}
                    onFileSelect={onFileSelect}
                    onRetry={groupRetry}
                  />
                );
              })}
              {isRunning && messages.length > 0 && messages[messages.length - 1].role === "user" && (
                <Message from="assistant">
                  <MessageContent>
                    <Shimmer duration={5} className="text-sm">Working on it...</Shimmer>
                  </MessageContent>
                </Message>
              )}
            </>
          )}
        </ConversationContent>
        <ConversationScrollButton />
      </Conversation>

      <div className="mx-auto w-full max-w-4xl px-4 pb-4 pt-2">
        {retryIndicator && (
          <div className="mb-2">
            <RetryBanner indicator={retryIndicator} />
          </div>
        )}
        <ChatComposer
          onSend={onSend}
          onStop={onStop}
          isRunning={isRunning}
          disabled={disabled}
          tokenUsage={tokenUsage}
        />
      </div>
    </div>
  );
}

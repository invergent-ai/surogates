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
} from "../ai-elements/conversation";
import {
  Message,
  MessageContent,
  MessageResponse,
} from "../ai-elements/message";
import {
  Reasoning,
  ReasoningContent,
  ReasoningTrigger,
} from "../ai-elements/reasoning";
import {
  Timeline,
  TimelineContent,
  TimelineHeader,
  TimelineIndicator,
  TimelineItem,
  TimelineSeparator,
} from "../reui/timeline";
import { Shimmer } from "../ai-elements/shimmer";
import { ToolCallBlock } from "./tool-call-block";
import { statusColorClass, effectiveStatus, toolErrorSummary, parseArgs } from "./tools/shared";
import { ChatMessage } from "./chat-message";
import { ChatComposer } from "./chat-composer";
import { ArtifactBlock } from "./artifacts/artifact-block";
import { ErrorMessage } from "./error-message";
import { cn } from "../../lib/utils";
import { AlertTriangle, ChevronDown, ChevronRight, MessageSquareIcon } from "lucide-react";
import { useState } from "react";
import type {
  AgentChatImageAttachment,
  ChatMessage as ChatMessageType,
  RetryIndicator,
  ToolCallInfo,
  TokenUsage,
} from "../../types";
import type { ArtifactKind } from "../../types";

interface ChatThreadProps {
  sessionId: string | null;
  messages: ChatMessageType[];
  isRunning: boolean;
  isLoadingHistory?: boolean;
  onSend: (text: string, images?: AgentChatImageAttachment[]) => void;
  onStop: () => void;
  onFileSelect?: (path: string) => void;
  disabled?: boolean;
  disabledReason?: string;
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
  | { kind: "tool"; key: string; tc: ToolCallInfo; resolvedArtifactName?: string }
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
 *
 * ``artifactFallbacks`` maps a ``create_artifact`` tool-call id to the
 * name of the matching ``artifact.created`` system message that has
 * already landed. Used to backfill the timeline label during the brief
 * race where ``tool.result`` arrives before the tool-call args have
 * fully streamed.
 */
function messageToEntries(
  msg: ChatMessageType,
  isLast: boolean,
  artifactFallbacks: Record<string, string>,
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
      if (tc.toolName === "create_artifact") {
        // Skip the entry until either ``args.name`` parses or the matching
        // ``artifact.created`` system message has landed. Avoids the brief
        // "empty green dot" race where ``tool.result`` flips status to
        // complete before the tool-call args have fully streamed in.
        const resolvedName = parseArgs<{ name?: string }>(tc.args)?.name
          ?? artifactFallbacks[tc.id];
        const status = effectiveStatus(tc);
        if (!resolvedName && status !== "error") continue;
        entries.push({
          kind: "tool",
          key: tc.id,
          tc,
          resolvedArtifactName: resolvedName,
        });
        continue;
      }
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
      <div className="mx-auto my-2 w-full max-w-4xl">
        <ErrorMessage errorInfo={message.errorInfo} onRetry={onRetry} />
      </div>
    );
  }

  return null;
}

// ── Cancelled tool row (parallel-batch sibling-error cancellation) ──

function cancelledToolLabel(toolName: string): string {
  const map: Record<string, string> = {
    terminal: "Command",
    execute_code: "Execute Code",
    read_file: "Read",
    write_file: "Write",
    patch: "Patch",
    search_files: "Search Files",
    list_files: "List Files",
    web_search: "Web Search",
    web_extract: "Web Fetch",
    web_crawl: "Web Crawl",
    session_search: "Session Search",
    memory: "Memory",
    todo: "Todo",
    skills_list: "Skills",
    skill_view: "Skill",
    consult_expert: "Expert",
    delegate_task: "Delegate",
    clarify: "Clarify",
    process: "Process",
    create_artifact: "Artifact",
  };
  return map[toolName] ?? toolName;
}

function CancelledToolRow({ tc }: { tc: ToolCallInfo }) {
  return (
    <div className="flex items-center gap-1.5 text-sm">
      <span className="font-semibold text-muted-foreground">
        {cancelledToolLabel(tc.toolName)}
      </span>
      <span className="text-muted-foreground/70">
        Cancelled
      </span>
    </div>
  );
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
    const rawStatus = effectiveStatus(entry.tc);
    // Failed ``create_artifact`` calls are almost always transient
    // (malformed shape, stringified spec) and immediately retried by
    // the model.  We hide the failure from the UI so the user sees a
    // single coherent "Creating artifact… → Created" flow instead of a
    // red error followed by a successful retry.  Treat the error as
    // "still running" for both the timeline dot and the failure text.
    const hideArtifactFailure =
      rawStatus === "error" && entry.tc.toolName === "create_artifact";
    const indicatorStatus = hideArtifactFailure ? "running" : rawStatus;
    const failureSummary =
      rawStatus === "error" && !hideArtifactFailure
        ? toolErrorSummary(entry.tc.result)
        : "";
    return (
      <TimelineItem step={step}>
        <TimelineHeader>
          <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
          <TimelineIndicator
            className={cn("size-2 border-none", statusColorClass(indicatorStatus))}
          />
        </TimelineHeader>
        <TimelineContent>
          {entry.tc.cancelled ? (
            <CancelledToolRow tc={entry.tc} />
          ) : (
            <div className="space-y-1">
              <ToolCallBlock
                tc={entry.tc}
                resolvedArtifactName={entry.resolvedArtifactName}
                onFileSelect={onFileSelect}
              />
              {failureSummary ? (
                <div className="max-w-full truncate text-xs text-destructive" title={failureSummary}>
                  {failureSummary}
                </div>
              ) : null}
            </div>
          )}
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
        <Shimmer duration={3} spread={3} className="text-sm">Working on it...</Shimmer>
      </TimelineContent>
    </TimelineItem>
  );
}

// ── Assistant message group (single Timeline) ────────────────────────

function AssistantGroup({
  messages,
  lastGlobalIndex,
  totalMessages,
  isRunning,
  sessionId,
  artifactFallbacks,
  onFileSelect,
  onRetry,
}: {
  messages: ChatMessageType[];
  lastGlobalIndex: number;
  totalMessages: number;
  isRunning: boolean;
  sessionId: string | null;
  artifactFallbacks: Record<string, string>;
  onFileSelect?: (path: string) => void;
  onRetry?: () => Promise<void>;
}) {
  const entries: TimelineEntry[] = [];
  for (let i = 0; i < messages.length; i++) {
    const isLast = i === messages.length - 1
      && lastGlobalIndex === totalMessages - 1;
    entries.push(...messageToEntries(messages[i], isLast, artifactFallbacks));
  }

  // Whenever this is the tail assistant group and the session is still
  // running, append a "Working on it..." row unless something visible is
  // already in progress (a running tool, or messageToEntries already added
  // a thinking entry for an empty streaming turn). This covers both the
  // mid-stream pause between text and the next tool call AND the gap
  // between LLM iterations after a turn has fully completed.
  const isTailGroup = lastGlobalIndex === totalMessages - 1;
  const lastEntry = entries[entries.length - 1];
  const hasRunningTool = entries.some(
    (e) => e.kind === "tool" && e.tc.status === "running",
  );
  const tailMsg = messages[messages.length - 1];
  if (
    isTailGroup
    && isRunning
    && !hasRunningTool
    && lastEntry?.kind !== "thinking"
  ) {
    entries.push({ kind: "thinking", key: `${tailMsg.id}-tail-thinking` });
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
  isLoadingHistory = false,
  onSend,
  onStop,
  onFileSelect,
  disabled = false,
  disabledReason,
  tokenUsage,
  retryIndicator,
  onRetry,
}: ChatThreadProps) {
  const groups = useMemo(() => groupMessages(messages), [messages]);

  // Pair ``create_artifact`` tool calls to their matching
  // ``artifact.created`` system messages by emission order across the
  // whole thread (the events live in different group-buckets, but the
  // 1:1 ordering is stable). The map is keyed by tool-call id so a
  // ``create_artifact`` whose args have not yet streamed in can still
  // render with the artifact's name as a fallback label.
  //
  // Errored and cancelled calls never emit ``artifact.created``, so they
  // are excluded from the pairing — including them would slip the index
  // and attribute a successful call's artifact name to a failed sibling.
  const artifactFallbacks = useMemo<Record<string, string>>(() => {
    const fallbacks: Record<string, string> = {};
    const createArtifactToolCallIds: string[] = [];
    let artifactIdx = 0;
    for (const msg of messages) {
      if (msg.role === "assistant" && msg.toolCalls) {
        for (const tc of msg.toolCalls) {
          if (tc.toolName === "create_artifact"
              && !tc.cancelled
              && tc.status !== "error") {
            createArtifactToolCallIds.push(tc.id);
          }
        }
      } else if (msg.role === "system" && msg.systemKind === "artifact") {
        const tcId = createArtifactToolCallIds[artifactIdx];
        if (tcId) {
          const { name } = unpackArtifactMeta(msg.systemMeta, msg.content);
          if (name) fallbacks[tcId] = name;
        }
        artifactIdx += 1;
      }
    }
    return fallbacks;
  }, [messages]);

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
    <div className="flex flex-1 flex-col overflow-hidden bg-background text-sm">
      <Conversation className="relative flex-1 min-h-0">
        <ConversationContent className="mx-auto w-full max-w-4xl">
          {messages.length === 0 && isLoadingHistory ? (
            <ConversationEmptyState
              icon={<MessageSquareIcon className="size-8 opacity-40" />}
              title="Loading conversation"
              description="Fetching the session history."
            />
          ) : messages.length === 0 && !disabled ? (
            <ConversationEmptyState
              icon={<MessageSquareIcon className="size-8 opacity-40" />}
              title="Start a conversation"
              description="Ask me anything — I can search, analyze, write code, and more."
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
                    isRunning={isRunning}
                    sessionId={sessionId}
                    artifactFallbacks={artifactFallbacks}
                    onFileSelect={onFileSelect}
                    onRetry={groupRetry}
                  />
                );
              })}
              {isRunning && messages.length > 0 && messages[messages.length - 1].role === "user" && (
                <Message from="assistant">
                  <MessageContent>
                    <Shimmer duration={3} spread={3} className="text-sm">Working on it...</Shimmer>
                  </MessageContent>
                </Message>
              )}
            </>
          )}
        </ConversationContent>
        <ConversationScrollButton />
      </Conversation>

      <div className="mx-auto w-full max-w-4xl px-6 pb-5 pt-3">
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
          disabledReason={disabledReason}
          tokenUsage={tokenUsage}
        />
      </div>
    </div>
  );
}

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Custom chat thread — uses ai-elements Conversation + Message
// with a compact, Claude Code-inspired layout.
//
import { useEffect, useMemo, useState } from "react";
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
import { BrowserActivityGroup } from "../browser/browser-activity-group";
import { ToolCallBlock } from "./tool-call-block";
import { WebSearchGroupBlock } from "./tools/web-search-tool";
import { TodoToolBlock } from "./tools/todo-tool";
import { AskUserQuestionToolBlock } from "./tools/ask-user-question-tool";
import { parseTerminalResult } from "./tools/terminal-tool";
import { statusColorClass, effectiveStatus, toolErrorSummary, parseArgs } from "./tools/shared";
import { ChatMessage } from "./chat-message";
import { ChatComposer } from "./chat-composer";
import { ResearchSourcesPanel } from "../research/research-sources-panel";
import { TurnFeedback } from "./turn-feedback";
import { useSmoothStream } from "./use-smooth-stream";
import { stripAndParseNextAction } from "../../lib/next-action";
import { ArtifactBlock } from "./artifacts/artifact-block";
import { ErrorMessage } from "./error-message";
import { TurnSummaryCard } from "./turn-summary-card";
import { cn } from "../../lib/utils";
import {
  AlertTriangle,
  ArrowRight,
  BookOpenIcon,
  ChevronDown,
  ChevronRight,
  CircleCheckIcon,
  ClockIcon,
  FileEditIcon,
  FileTextIcon,
  GlobeIcon,
  ListIcon,
  MessageSquareIcon,
  PenLineIcon,
  SearchIcon,
  SparklesIcon,
  TerminalIcon,
  WrenchIcon,
  type LucideIcon,
} from "lucide-react";
import type {
  AgentChatImageAttachment,
  AgentChatPendingAttachment,
  AgentChatResearchSource,
  AgentChatTurnArtifactRef,
  ChatMessage as ChatMessageType,
  RetryIndicator,
  ToolCallInfo,
  TokenUsage,
} from "../../types";
import type { ArtifactKind } from "../../types";
import type { ChatComposerError } from "./chat-composer";

interface ChatThreadProps {
  sessionId: string | null;
  messages: ChatMessageType[];
  isRunning: boolean;
  /** True once the session has ended terminally. Kept in the prop
   *  contract for callers that already thread runtime state through
   *  ChatThread. */
  terminal: boolean;
  isLoadingHistory?: boolean;
  onSend: (
    text: string,
    images?: AgentChatImageAttachment[],
    attachments?: AgentChatPendingAttachment[],
  ) => void | Promise<void>;
  onStop: () => void | Promise<void>;
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
  // Forwarded to the composer to surface client-side rejections.  The
  // SDK does not ship a toast subsystem; callers (e.g. surogate-ops)
  // wire their own.
  onComposerError?: (err: ChatComposerError) => void;

  // Pane toggle wiring — forwarded to the composer's tools row. AgentChat
  // owns the visibility state; the composer renders the buttons.
  showBrowser?: boolean;
  onToggleBrowser?: () => void;
  showWorkspace?: boolean;
  onToggleWorkspace?: () => void;
  canShowBrowser?: boolean;
  canShowWorkspace?: boolean;
  // Simple/Expert view-mode toggle — also threaded into AssistantGroup
  // in B9 to gate the actual render path. When omitted, the composer
  // hides the toggle and the thread defaults to Simple internally.
  viewMode?: "simple" | "expert";
  onViewModeChange?: (mode: "simple" | "expert") => void;

  // When true, the composer's slash menu includes ``/deep-research``.
  // Forwarded as-is to ChatComposer.
  deepResearchEnabled?: boolean;

  // Curated research sources from the active deep-research workflow.
  // When non-empty an expandable "Sources" strip renders above the
  // composer; ``[S#]`` citation chips deep-link to entries here.
  researchSources?: AgentChatResearchSource[];

  // When true the per-turn recap card is suppressed.  Sub-agent
  // sessions (delegation / worker / task children) are consumed by
  // their parent's LLM, not by a human reader -- the recap card just
  // takes screen real estate without serving anyone.  Set by
  // ``AgentChat`` from the existing ``isSubAgentSession`` helper.
  hideTurnSummary?: boolean;
}

// ── Timeline item types ──────────────────────────────────────────────

type TimelineEntry =
  | { kind: "reasoning"; key: string; reasoning: string; isStreaming: boolean }
  | { kind: "tool"; key: string; tc: ToolCallInfo; resolvedArtifactName?: string }
  | { kind: "browser_activity"; key: string; calls: ToolCallInfo[] }
  | { kind: "web_search_group"; key: string; calls: ToolCallInfo[] }
  | {
      kind: "text";
      key: string;
      content: string;
      isStreaming: boolean;
      msg: ChatMessageType;
      isFinalTurnText?: boolean;
    }
  /**
   * Italic narration line rendered above the iteration's tool calls.
   * Carries the assistant's prose preamble ("I'll fetch the weather
   * data...") that the model emits before its ``tool_calls``.  Without
   * this entry the prose would be buried inside the reasoning
   * collapsible -- this surfaces it as the iteration's natural
   * narration line.  When the model emits a ``<next_action>`` footer
   * the renderer prefers it over the heuristic.
   */
  | { kind: "narration"; key: string; text: string; isStreaming: boolean }
  | { kind: "thinking"; key: string }
  | { kind: "skill_invoked"; key: string; skill: string; stagedAt: string | null }
  | { kind: "browser_marker"; key: string; content: string; warning: boolean }
  | {
      kind: "artifact";
      key: string;
      artifactId: string;
      name: string;
      artifactKind: ArtifactKind;
      version: number;
      /**
       * Set when the artifact was propagated up from a delegated
       * child session.  ``ArtifactBlock`` uses it to fetch the spec
       * from the session that actually owns the S3 prefix; the chat
       * thread's own session id would 404 for a propagated artifact.
       */
      originatingSessionId: string | null;
    };

const WORKING_ON_IT_DELAY_MS = 250;

/**
 * ``ask_user_question`` is the one tool whose accompanying assistant
 * ``content`` is a full user-facing message body (e.g. a proposed
 * design the user must approve) rather than the throwaway preamble
 * other tool calls emit.  Such turns must render their body in full —
 * collapsing it to a one-line narration drops the very content the user
 * is being asked to act on.
 */
function hasUserFacingAskContent(msg: ChatMessageType): boolean {
  return !!msg.toolCalls?.some((tc) => tc.toolName === "ask_user_question");
}

/**
 * Whether the thread is parked waiting on the user rather than working.
 *
 * The reducer keeps ``isRunning`` true while an ``ask_user_question``
 * tool call is pending, but the agent has handed control back to the
 * user — so the "Working on it…" indicator would be misleading.
 *
 * We scan backward from the end, skipping trailing system markers the
 * reducer appends after the assistant turn (artifact.created,
 * skill.invoked, browser.*) — a turn that emits ``create_artifact`` +
 * ``ask_user_question`` lands its artifact marker as the literal tail,
 * so a tail-only check would miss the still-pending ask. The first
 * assistant turn we reach decides it; a ``user`` message after the ask
 * means the user moved on, so it's no longer awaiting.
 */
function isAwaitingUserInput(messages: ChatMessageType[]): boolean {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === "user") return false;
    if (m.role !== "assistant") continue; // skip trailing system markers
    return !!m.toolCalls?.some(
      (tc) => tc.toolName === "ask_user_question" && tc.status === "running",
    );
  }
  return false;
}

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
      const { artifactId, name, kind, version, originatingSessionId } =
        unpackArtifactMeta(msg.systemMeta, msg.content);
      return [{
        kind: "artifact",
        key: msg.id,
        artifactId,
        name,
        artifactKind: kind,
        version,
        originatingSessionId,
      }];
    }
    if (
      msg.systemKind === "browser_marker"
      || msg.systemKind === "browser_marker_warning"
    ) {
      return [{
        kind: "browser_marker",
        key: msg.id,
        content: msg.content,
        warning: msg.systemKind === "browser_marker_warning",
      }];
    }
    return [];
  }

  const entries: TimelineEntry[] = [];
  const hasToolCalls = !!(msg.toolCalls && msg.toolCalls.length > 0);
  const hasContent = !!(msg.content && msg.content !== msg.reasoning);
  const isStreaming = msg.status === "streaming" && isLast;

  // When tool calls are present the content text is just a preamble
  // ("I'll run both tasks in parallel...").  We surface it as its own
  // ``narration`` entry rendered as an italic line above the tool
  // calls, instead of folding it into the reasoning collapsible.  The
  // reasoning collapsible then carries pure chain-of-thought; the
  // narration is always visible without expanding.
  const effectiveHasContent = hasContent && !hasToolCalls;

  if (msg.reasoning) {
    entries.push({
      kind: "reasoning",
      key: `${msg.id}-reasoning`,
      reasoning: msg.reasoning,
      isStreaming: isStreaming && !effectiveHasContent && !hasToolCalls,
    });
  }

  if (hasContent && hasToolCalls) {
    if (hasUserFacingAskContent(msg)) {
      // The body is the message the user must act on (the proposed
      // design behind the question widget), not a preamble — render it
      // in full above the tool calls instead of as a narration line.
      // Not streamed: the body has fully arrived by the time the ask
      // tool call lands, so a char-reveal would only spin a perpetual
      // requestAnimationFrame loop while the turn is parked on the user.
      entries.push({
        kind: "text",
        key: `${msg.id}-text`,
        content: msg.content,
        isStreaming: false,
        msg,
      });
    } else {
      entries.push({
        kind: "narration",
        key: `${msg.id}-narration`,
        text: msg.content,
        isStreaming: isStreaming && !effectiveHasContent,
      });
    }
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
      if (tc.toolName === "terminal" && tc.status !== "running") {
        const parsed = parseTerminalResult(tc.result, tc.args);
        if (parsed && (parsed.exit_code !== 0 || parsed.error)) continue;
      }
      entries.push({ kind: "tool", key: tc.id, tc });
    }
  }

  if (effectiveHasContent) {
    entries.push({
      kind: "text",
      key: `${msg.id}-text`,
      content: msg.content,
      isStreaming,
      msg,
    });
  }

  return entries;
}

function groupBrowserActivityEntries(entries: TimelineEntry[]): TimelineEntry[] {
  const grouped: TimelineEntry[] = [];
  let buffer: ToolCallInfo[] = [];

  const flush = () => {
    if (buffer.length === 0) return;
    grouped.push({
      kind: "browser_activity",
      key: `browser-activity-${buffer[0]?.id}-${buffer[buffer.length - 1]?.id}`,
      calls: buffer,
    });
    buffer = [];
  };

  for (const entry of entries) {
    if (entry.kind === "tool" && isBrowserToolCall(entry.tc)) {
      buffer.push(entry.tc);
      continue;
    }
    flush();
    grouped.push(entry);
  }

  flush();
  return grouped;
}

function isBrowserToolCall(call: ToolCallInfo): boolean {
  return call.toolName.startsWith("browser_");
}

function groupWebSearchEntries(entries: TimelineEntry[]): TimelineEntry[] {
  const grouped: TimelineEntry[] = [];
  let buffer: ToolCallInfo[] = [];

  const flush = () => {
    if (buffer.length === 0) return;
    if (buffer.length === 1) {
      grouped.push({ kind: "tool", key: buffer[0].id, tc: buffer[0] });
    } else {
      grouped.push({
        kind: "web_search_group",
        key: `web-search-${buffer[0].id}-${buffer[buffer.length - 1].id}`,
        calls: buffer,
      });
    }
    buffer = [];
  };

  for (const entry of entries) {
    if (entry.kind === "tool" && entry.tc.toolName === "web_search") {
      buffer.push(entry.tc);
      continue;
    }
    flush();
    grouped.push(entry);
  }

  flush();
  return grouped;
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
): {
  artifactId: string;
  name: string;
  kind: ArtifactKind;
  version: number;
  /**
   * When the artifact was produced by a delegated child session and
   * propagated up the chain, this is the session that actually owns
   * the spec in S3.  The ArtifactBlock fetches via this id so the
   * GET /api/sessions/{id}/artifacts/{artifact_id} route resolves
   * (the chat's own session id would 404 because the spec lives
   * under the writer's prefix, not the planner's or root's).
   */
  originatingSessionId: string | null;
} {
  const meta = systemMeta ?? {};
  return {
    artifactId: (meta.artifact_id as string) ?? "",
    name: (meta.name as string) ?? fallbackName,
    kind: (meta.kind as ArtifactKind) ?? "markdown",
    version: (meta.version as number) ?? 1,
    originatingSessionId:
      (meta.originating_session_id as string | undefined) ?? null,
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
      <div className="my-2 flex items-center gap-2 px-4 text-xs text-foreground/60 ">
        <span className="size-2 rounded-full bg-emerald-500" />
        <span>
          <span className="font-semibold text-foreground">Running skill{" "}</span>
          <span className="text-foreground/60 truncate">{skill}</span>
        </span>
      </div>
    );
  }

  if (message.systemKind === "artifact" && sessionId) {
    const unpacked = unpackArtifactMeta(message.systemMeta, message.content);
    return (
      <ArtifactBlock
        sessionId={unpacked.originatingSessionId ?? sessionId}
        artifactId={unpacked.artifactId}
        name={unpacked.name}
        kind={unpacked.kind}
        version={unpacked.version}
      />
    );
  }

  if (
    message.systemKind === "browser_marker"
    || message.systemKind === "browser_marker_warning"
  ) {
    const warning = message.systemKind === "browser_marker_warning";
    return (
      <div
        className={cn(
          "my-2 flex items-center gap-2 px-4 text-xs",
          warning ? "text-amber-600" : "text-foreground/60",
        )}
        role={warning ? "alert" : "status"}
      >
        {warning ? (
          <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
        ) : (
          <span className="size-2 rounded-full bg-foreground/30" />
        )}
        <span className="truncate">{message.content}</span>
      </div>
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
    execute_code: "Execute code",
    read_file: "Read",
    write_file: "Write",
    patch: "Patch",
    search_files: "Search files",
    list_files: "List files",
    web_search: "Web search",
    web_extract: "Web fetch",
    web_crawl: "Web crawl",
    session_search: "Session search",
    memory: "Memory",
    todo: "Todo",
    skills_list: "Skills",
    skill_view: "Skill",
    consult_expert: "Expert",
    delegate_task: "Delegate task",
    ask_user_question: "Ask User Question",
    process: "Process",
    create_artifact: "Create artifact",
    // ``Research memory`` reads as a noun ("memory of research") and
    // makes the shimmer awkward ("Running Research memory…").  The
    // one-liner renderer shows just ``Research`` as the label, so
    // mirror that here so the live shimmer matches the row that
    // replaces it on completion.
    research_memory: "Research",
    research_outline: "Research",
  };
  return map[toolName] ?? toolName;
}

function CancelledToolRow({ tc }: { tc: ToolCallInfo }) {
  return (
    <div className="flex items-center gap-1.5 text-sm">
      <span className="font-semibold text-foreground/60">
        {cancelledToolLabel(tc.toolName)}
      </span>
      <span className="text-foreground/60/70">
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
    return <ReasoningEntry entry={entry} step={step} />;
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

  if (entry.kind === "browser_activity") {
    return (
      <TimelineItem step={step}>
        <TimelineHeader>
          <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
          <TimelineIndicator className="size-2 border-none bg-amber-500" />
        </TimelineHeader>
        <TimelineContent>
          <BrowserActivityGroup calls={entry.calls} />
        </TimelineContent>
      </TimelineItem>
    );
  }

  if (entry.kind === "web_search_group") {
    const allComplete = entry.calls.every((tc) => tc.status === "complete");
    const anyRunning = entry.calls.some((tc) => tc.status === "running");
    const indicatorClass = anyRunning
      ? "bg-emerald-500 animate-pulse"
      : allComplete
        ? "bg-emerald-500"
        : "bg-red-500";
    return (
      <TimelineItem step={step}>
        <TimelineHeader>
          <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
          <TimelineIndicator className={cn("size-2 border-none", indicatorClass)} />
        </TimelineHeader>
        <TimelineContent>
          <WebSearchGroupBlock tcs={entry.calls} />
        </TimelineContent>
      </TimelineItem>
    );
  }

  if (entry.kind === "text") {
    return <TextEntry entry={entry} step={step} />;
  }

  if (entry.kind === "narration") {
    return <NarrationEntry entry={entry} step={step} />;
  }

  if (entry.kind === "browser_marker") {
    return (
      <TimelineItem step={step}>
        <TimelineHeader>
          <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
          <TimelineIndicator
            className={cn(
              "size-2 border-none",
              entry.warning ? "bg-amber-500" : "bg-foreground/30",
            )}
          />
        </TimelineHeader>
        <TimelineContent>
          <div
            className={cn(
              "flex items-center gap-1.5 py-1 text-sm",
              entry.warning ? "text-amber-600" : "text-foreground/60",
            )}
            role={entry.warning ? "alert" : "status"}
          >
            {entry.warning && (
              <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
            )}
            <span>{entry.content}</span>
          </div>
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
            <span className="text-foreground/60 truncate">
              {entry.skill}
            </span>
            {entry.stagedAt && (
              <span className="text-xs text-foreground/60/70 ">
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
              sessionId={entry.originatingSessionId ?? sessionId}
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

// ── Streamed-text timeline entries ───────────────────────────────────

function ReasoningEntry({
  entry,
  step,
}: {
  entry: Extract<TimelineEntry, { kind: "reasoning" }>;
  step: number;
}) {
  const reasoning = useSmoothStream(entry.reasoning, entry.isStreaming);
  return (
    <TimelineItem step={step}>
      <TimelineHeader>
        <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
        <TimelineIndicator className="size-2" />
      </TimelineHeader>
      <TimelineContent>
        <Reasoning isStreaming={entry.isStreaming}>
          <ReasoningTrigger />
          <ReasoningContent>{reasoning}</ReasoningContent>
        </Reasoning>
      </TimelineContent>
    </TimelineItem>
  );
}

// Renders the assistant's prose preamble ("I'll fetch the weather
// next.") above its tool calls as a small italic line.  When the
// model emitted a structured ``<next_action>`` footer the body of
// that footer wins; otherwise we fall back to the first-sentence
// heuristic (language-agnostic).  Hidden when the model marked the
// turn as ``done`` so the final answer doesn't carry a trailing
// whisper, and hidden when the preamble produced no usable text.
function NarrationEntry({
  entry,
  step,
}: {
  entry: Extract<TimelineEntry, { kind: "narration" }>;
  step: number;
}) {
  const text = useSmoothStream(entry.text, entry.isStreaming);
  const { action, inferredNarration } = stripAndParseNextAction(text);
  const line = action
    ? action.body.trim().toLowerCase() === "done"
      ? null
      : action.body
    : inferredNarration;
  if (!line) return null;
  return (
    <TimelineItem step={step}>
      <TimelineHeader>
        <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
        <TimelineIndicator className="size-2 border-none bg-foreground/30" />
      </TimelineHeader>
      <TimelineContent>
        <p>{line}</p>
      </TimelineContent>
    </TimelineItem>
  );
}

function TextEntry({
  entry,
  step,
}: {
  entry: Extract<TimelineEntry, { kind: "text" }>;
  step: number;
}) {
  const content = useSmoothStream(entry.content, entry.isStreaming);
  // Strip the harness ``<next_action>`` footer from the rendered
  // markdown.  No narration line here -- a text-only iteration IS
  // the final answer; the first sentence would duplicate the body.
  // Tool-call iterations get their narration via ``NarrationEntry``
  // (Expert mode) or the parent assistant-group renderer (Simple
  // mode), not here.
  const { cleaned } = stripAndParseNextAction(content);
  return (
    <TimelineItem step={step}>
      <TimelineHeader>
        <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
        <TimelineIndicator className="size-2 border-none bg-foreground/40" />
      </TimelineHeader>
      <TimelineContent>
        <MessageResponse>{cleaned}</MessageResponse>
        {entry.isFinalTurnText && entry.msg.status === "complete" && (
          <TurnFeedback msg={entry.msg} />
        )}
      </TimelineContent>
    </TimelineItem>
  );
}

// Simple-mode counterpart to TextEntry. Routes the final-answer text
// through ``useSmoothStream`` so character reveal pace stays even when
// the underlying delta cadence is bursty (one of the harness's larger
// chunks would otherwise pop in all at once). TurnFeedback only shows
// once the smoothed reveal has caught up to the complete message.
function SimpleFinalAnswer({
  text,
  isStreaming,
  tail,
}: {
  text: string;
  isStreaming: boolean;
  tail: ChatMessageType | undefined;
}) {
  const content = useSmoothStream(text, isStreaming);
  const revealComplete = content.length >= text.length;
  // Strip the next_action footer so it doesn't leak into the
  // rendered markdown.  No narration line here -- a final answer's
  // content IS the answer; surfacing its first sentence below would
  // duplicate the body during streaming.
  const { cleaned } = stripAndParseNextAction(content);
  return (
    <div>
      <MessageResponse>{cleaned}</MessageResponse>
      {tail?.status === "complete" && revealComplete && (
        <TurnFeedback msg={tail} />
      )}
    </div>
  );
}

// ── Simple-mode iteration row ────────────────────────────────────────
//
// IterationGroup wraps a SINGLE assistant message in the Simple view.
// It composes today's Expert-mode timeline pieces (messageToEntries +
// TimelineEntryItem, plus the existing browser/web-search sub-grouping)
// inside the collapsible body so we never duplicate per-tool renderers.
// See docs/superpowers/specs/2026-05-24-simple-chat-mode-design.md.

export interface IterationGroupProps {
  message: ChatMessageType;
  sessionId: string | null;
  /** Same map AssistantGroup builds for the Expert path — see
   *  ``ChatThread`` for the full derivation. */
  artifactFallbacks: Record<string, string>;
  onFileSelect?: (path: string) => void;
}

/**
 * Short, human-ish label derived from an iteration's tool calls when
 * no LLM-generated summary is available (summary_model unconfigured,
 * summarizer timed out, replay of pre-feature history, etc.).
 *
 * Returns ``null`` when nothing useful can be derived — caller renders
 * a generic "Iteration" pill or falls through to the shimmer.
 */
function deriveIterationLabel(message: ChatMessageType): string | null {
  // Internal/exploration tools and failed calls collapse into
  // background noise that Simple mode suppresses.
  const calls = visibleToolCalls(message);
  if (calls.length === 0) {
    return message.reasoning ? "Thought through the problem" : null;
  }
  if (calls.length === 1) {
    const tc = calls[0]!;
    return deriveSingleToolLabel(tc);
  }
  // Multiple tool calls: collapse same-tool runs, summarize mixed.
  const firstName = calls[0]!.toolName;
  const allSame = calls.every((tc) => tc.toolName === firstName);
  if (allSame) {
    const human = cancelledToolLabel(firstName);
    return `${human} × ${calls.length}`;
  }
  return `Used ${calls.length} tools`;
}

/**
 * Header label for a single-tool iteration. Most tools surface a
 * short detail (path basename, query, URL) so the header reads
 * "Read · landing.html". Tools whose detail is structurally noisy —
 * shell commands, arbitrary code blocks, raw memory keys — drop the
 * detail entirely and use a generic verb instead; the full detail
 * still appears in the expanded body via _toolRowLabel.
 */
function deriveSingleToolLabel(tc: ToolCallInfo): string {
  const name = cancelledToolLabel(tc.toolName);
  if (_HEADER_HIDES_DETAIL.has(tc.toolName)) {
    return _HEADER_GENERIC_VERB[tc.toolName] ?? name;
  }
  const detail = extractToolDetail(tc);
  return detail ? `${name} · ${detail}` : name;
}

const _HEADER_HIDES_DETAIL: ReadonlySet<string> = new Set([
  "terminal",
  "execute_code",
  "memory",
]);

const _HEADER_GENERIC_VERB: Record<string, string> = {
  terminal: "Ran a command",
  execute_code: "Executed code",
  memory: "Updated memory",
};

/**
 * Pull a short, human-readable detail string from a tool call's
 * arguments. Defensive against unparseable / partial JSON during
 * streaming.
 */
function extractToolDetail(tc: ToolCallInfo): string | null {
  const args = parseArgs<Record<string, unknown>>(tc.args);
  if (!args) return null;
  // Prefer the most-meaningful arg per tool family.
  const stringArg = (key: string): string | null => {
    const v = args[key];
    return typeof v === "string" && v.length > 0 ? v : null;
  };
  switch (tc.toolName) {
    case "read_file":
    case "write_file":
    case "patch":
    case "list_files":
    case "search_files": {
      const path = stringArg("path") ?? stringArg("file_path");
      return path ? lastPathSegment(path) : null;
    }
    case "terminal": {
      const cmd = stringArg("command");
      return cmd ? truncate(cmd, 40) : null;
    }
    case "execute_code": {
      const code = stringArg("code");
      return code ? truncate(code.split("\n")[0] ?? "", 40) : null;
    }
    case "web_search":
    case "web_crawl":
      return stringArg("query");
    case "web_extract": {
      const url = stringArg("url");
      if (!url) return null;
      // Hostname is much more readable in a one-line chip than the
      // full URL; falls back to the raw value for unparseable inputs
      // (file://, data:, etc.).
      try {
        return new URL(url).hostname.replace(/^www\./, "");
      } catch {
        return truncate(url, 40);
      }
    }
    case "skill_view":
    case "skill_manage":
      return stringArg("name") ?? stringArg("skill");
    case "create_artifact":
    case "consult_expert":
    case "delegate_task":
      return stringArg("name") ?? stringArg("task") ?? stringArg("title");
    case "memory":
      return stringArg("action") ?? stringArg("key");
    case "research_memory": {
      // ``add`` carries url+title; ``retrieve`` carries query; ``list``
      // carries nothing useful.  The Simple-mode row prefers a human
      // anchor over the raw action verb.
      const action = stringArg("action");
      if (action === "add") {
        const title = stringArg("title");
        if (title) return truncate(title, 60);
        const url = stringArg("url");
        if (url) {
          try {
            return new URL(url).hostname.replace(/^www\./, "");
          } catch {
            return truncate(url, 40);
          }
        }
        return null;
      }
      if (action === "retrieve") {
        return stringArg("query");
      }
      return action;
    }
    case "research_outline": {
      const action = stringArg("action");
      if (action === "set") {
        // Count level-2+ markdown headings in the outline so the row
        // surfaces "set outline (10 sections)" rather than the bare
        // tool name.  Mirrors ``outline_sections`` on the Python side.
        const outline = stringArg("outline");
        if (outline) {
          const sections = outline
            .split(/\r?\n/)
            .filter((line) => /^#{2,6}\s+\S/.test(line)).length;
          if (sections > 0) {
            return `${sections} ${sections === 1 ? "section" : "sections"}`;
          }
        }
        return "outline";
      }
      return action;
    }
    default:
      return null;
  }
}

function lastPathSegment(path: string): string {
  const cleaned = path.replace(/\/+$/, "");
  const idx = cleaned.lastIndexOf("/");
  return idx >= 0 ? cleaned.slice(idx + 1) : cleaned;
}

function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/**
 * Tools that are hidden from Simple mode. These are either pure
 * exploration ("listed the directory"), internal infrastructure
 * (browser_*, process), or scratchpad-style state changes (todo,
 * memory) that don't help the user understand what the agent
 * *did*. Users who care can switch to Expert mode to see them.
 */
const _SIMPLE_MODE_HIDDEN_TOOLS: ReadonlySet<string> = new Set([
  "list_files",
  "search_files",
  "session_search",
  "skills_list",
  "skill_view",
  "skill_manage",
  "process",
  "memory",
  // Shell commands and code execution are infrastructure plumbing the
  // user doesn't need to see when the goal is to know *what* the
  // agent accomplished, not *how*. Expert mode still has the full
  // command + output block.
  "terminal",
  "execute_code",
]);

function _isHiddenSimpleTool(tc: ToolCallInfo): boolean {
  if (_SIMPLE_MODE_HIDDEN_TOOLS.has(tc.toolName)) return true;
  // Browser tools are an internal sub-grouped activity in Expert
  // mode; in Simple mode we hide them outright.
  if (tc.toolName.startsWith("browser_")) return true;
  return false;
}

/**
 * The subset of an iteration's tool calls that should surface in
 * Simple mode: skips internal/infrastructure tools and any call
 * that errored or was cancelled. Used by every Simple-mode
 * consumer so the filter has a single source of truth.
 */
function visibleToolCalls(message: ChatMessageType): ToolCallInfo[] {
  return (message.toolCalls ?? []).filter((tc) => {
    if (_isHiddenSimpleTool(tc)) return false;
    const status = effectiveStatus(tc);
    if (status === "error" || status === "cancelled") return false;
    return true;
  });
}

/**
 * Whether an iteration is genuinely in-flight right now.
 *
 * ``message.status === "streaming"`` is NOT sufficient: the reducer
 * keeps tool-using assistant messages in the "streaming" state across
 * the entire rest of the turn (it never flips back to "complete" once
 * tool_calls are present). So a tool batch can finish — every tool
 * call resolved — while the message is still tagged "streaming".
 *
 * Live means: at least one tool call is still running, OR a text-only
 * iteration is mid-stream (no tools, status="streaming"). Everything
 * else counts as "done" for the purpose of swapping shimmer →
 * derived/summary label.
 */
function isIterationLive(message: ChatMessageType): boolean {
  const calls = message.toolCalls ?? [];
  if (calls.length > 0) {
    return calls.some((tc) => tc.status === "running");
  }
  return message.status === "streaming";
}

/**
 * Shimmer label for a live iteration. Derives a useful name from
 * currently-running tools so the user sees "Running List Files…"
 * instead of a generic "Working…" when context is available.
 */
function liveIterationLabel(message: ChatMessageType): string {
  // Only running tools that aren't hidden from Simple mode get
  // surfaced in the label — an internal list_files running on its
  // own should read as a quiet "Thinking…", not "Running List Files…".
  const running = (message.toolCalls ?? []).filter(
    (tc) => tc.status === "running" && !_isHiddenSimpleTool(tc),
  );
  if (running.length === 0) return "Thinking…";
  if (running.length === 1) {
    const tc = running[0]!;
    const name = cancelledToolLabel(tc.toolName);
    const detail = extractToolDetail(tc);
    return detail ? `Running ${name} · ${detail}…` : `Running ${name}…`;
  }
  const firstName = running[0]!.toolName;
  const allSame = running.every((tc) => tc.toolName === firstName);
  if (allSame) {
    return `Running ${cancelledToolLabel(firstName)} × ${running.length}…`;
  }
  return `Running ${running.length} tools…`;
}

/**
 * Icon-per-tool-family mapping for Simple-mode condensed rows. Keeps
 * the expanded view visually quiet — one icon + one prose line per
 * tool call — instead of the heavy per-tool blocks the Expert view
 * uses. Falls back to a generic wrench when a new tool name lands
 * before we've added a custom row for it.
 */
const _TOOL_ROW_ICON: Record<string, LucideIcon> = {
  read_file: FileTextIcon,
  write_file: FileTextIcon,
  patch: FileEditIcon,
  list_files: ListIcon,
  search_files: SearchIcon,
  terminal: TerminalIcon,
  execute_code: TerminalIcon,
  web_search: GlobeIcon,
  web_extract: GlobeIcon,
  web_crawl: GlobeIcon,
  session_search: SearchIcon,
  skill_view: BookOpenIcon,
  skills_list: BookOpenIcon,
  skill_manage: PenLineIcon,
  consult_expert: SparklesIcon,
  delegate_task: ArrowRight,
  create_artifact: FileTextIcon,
  memory: PenLineIcon,
  todo: ListIcon,
  research_memory: BookOpenIcon,
  research_outline: ListIcon,
};

function _toolRowIcon(toolName: string): LucideIcon {
  return _TOOL_ROW_ICON[toolName] ?? WrenchIcon;
}

/**
 * Verb-first prose line for a tool call ("Edited landing.html",
 * "Read the frontend-design skill"). Falls back to the human tool
 * name when we can't extract a useful detail from the args.
 */
function _toolRowLabel(tc: ToolCallInfo): string {
  const detail = extractToolDetail(tc);
  switch (tc.toolName) {
    case "read_file":
      return detail ? `Read ${detail}` : "Read a file";
    case "write_file":
      return detail ? `Wrote ${detail}` : "Wrote a file";
    case "patch":
      return detail ? `Edited ${detail}` : "Edited a file";
    case "list_files":
      return detail ? `Listed ${detail}` : "Listed files";
    case "search_files":
      return detail ? `Searched files for "${detail}"` : "Searched files";
    case "terminal":
      // Raw shell commands carry too much noise (paths, escapes,
      // chained pipes) to read well even as a body row. The Expert
      // view has the full block with output if the user needs it.
      return "Ran a command";
    case "execute_code":
      return "Executed code";
    case "web_search":
      return detail ? `Searched the web for "${detail}"` : "Searched the web";
    case "web_crawl":
      return detail ? `Crawled "${detail}"` : "Crawled the web";
    case "web_extract":
      return detail ? `Fetched ${detail}` : "Fetched a page";
    case "session_search":
      return detail ? `Searched session for "${detail}"` : "Searched session";
    case "skill_view":
      return detail ? `Read the ${detail} skill` : "Read a skill";
    case "skills_list":
      return "Listed available skills";
    case "skill_manage":
      return detail ? `Updated skill ${detail}` : "Managed skills";
    case "consult_expert":
      return detail ? `Consulted ${detail} expert` : "Consulted an expert";
    case "delegate_task":
      return detail ? `Delegated: ${detail}` : "Delegated a task";
    case "create_artifact":
      return detail ? `Created artifact "${detail}"` : "Created an artifact";
    case "memory":
      return detail ? `Memory ${detail}` : "Updated memory";
    case "todo":
      return detail ? `Todo ${detail}` : "Updated todo list";
    case "research_memory": {
      // ``detail`` already encodes the action's anchor (title /
      // hostname for add, query for retrieve, raw verb otherwise);
      // wrap it in the verb-first prose the rest of the row family
      // uses.
      const args = parseArgs<{ action?: string }>(tc.args);
      const action = args?.action;
      if (action === "add") {
        return detail ? `Stored source "${detail}"` : "Stored a source";
      }
      if (action === "retrieve") {
        return detail
          ? `Retrieved sources for "${detail}"`
          : "Retrieved sources";
      }
      if (action === "list") {
        return "Listed sources";
      }
      return "Updated research memory";
    }
    case "research_outline": {
      const args = parseArgs<{ action?: string }>(tc.args);
      const action = args?.action;
      if (action === "set") {
        return detail ? `Updated outline (${detail})` : "Updated outline";
      }
      if (action === "get") {
        return "Read outline";
      }
      return "Touched outline";
    }
    default:
      return cancelledToolLabel(tc.toolName);
  }
}

interface SimpleDetailRowProps {
  icon: LucideIcon;
  children: React.ReactNode;
}

function SimpleDetailRow({ icon: Icon, children }: SimpleDetailRowProps) {
  return (
    <div className="flex items-start gap-3 text-sm">
      <Icon
        className="mt-0.5 size-4 shrink-0 text-foreground/60"
        aria-hidden
      />
      <div className="min-w-0 flex-1 leading-relaxed text-foreground/60">
        {children}
      </div>
    </div>
  );
}

/**
 * Reasoning text rendered as paragraphs. By default we show the
 * first ``previewParagraphs`` paragraphs (split on blank lines) plus
 * a "Show more" link; clicking expands to the full text with a
 * matching "Show less" link.
 *
 * Paragraph split: blank-line separated when present, otherwise one
 * paragraph per source line (rare — most reasoning blocks already
 * use blank-line paragraph breaks).
 */
function ClampedReasoning({
  text,
  previewParagraphs = 2,
}: {
  text: string;
  previewParagraphs?: number;
}) {
  const paragraphs = splitReasoningParagraphs(text);
  const [expanded, setExpanded] = useState(false);
  const hasMore = paragraphs.length > previewParagraphs;
  const visible = expanded || !hasMore
    ? paragraphs
    : paragraphs.slice(0, previewParagraphs);
  return (
    <div className="space-y-2">
      {visible.map((paragraph, i) => (
        <p key={i} className="whitespace-pre-wrap">{paragraph}</p>
      ))}
      {hasMore && (
        <button
          type="button"
          onClick={() => setExpanded((prev) => !prev)}
          className="text-xs font-medium text-foreground hover:text-foreground/70 cursor-pointer"
        >
          {expanded ? "Show less" : "Show more..."}
        </button>
      )}
    </div>
  );
}

function splitReasoningParagraphs(text: string): string[] {
  const trimmed = text.trim();
  if (!trimmed) return [];
  const blankSplit = trimmed
    .split(/\n\s*\n+/)
    .map((p) => p.trim())
    .filter((p) => p.length > 0);
  if (blankSplit.length > 1) return blankSplit;
  // Fall back to one-paragraph-per-line so models that emit single
  // newlines still get a clamp boundary.
  return trimmed
    .split(/\n+/)
    .map((p) => p.trim())
    .filter((p) => p.length > 0);
}

/**
 * Condensed Simple-mode expansion of an iteration: one icon + prose
 * row per tool call, plus the reasoning text and a "Done" footer
 * when the iteration completed successfully.
 *
 * Deliberately DOES NOT reuse the Expert per-tool blocks (Patch
 * diffs, file viewers, tooltips) — Simple mode trades detail for
 * quiet. Users who want the heavy blocks switch to Expert.
 */
function IterationExpanded({
  message,
}: IterationGroupProps) {
  // Internal tools and failed calls are filtered out of the visible
  // list — the row vanishes entirely instead of exposing infra noise
  // ("Listed .", "Searched files", failed retries, browser_* steps).
  const calls = visibleToolCalls(message);
  const reasoning = (message.reasoning ?? "").trim();
  // "Done" reflects the user-visible state. If only hidden tools are
  // still running, the iteration looks complete to the user; we don't
  // want a stuck spinner on rows that are silent from their POV.
  const anyVisibleRunning = calls.some((tc) => tc.status === "running");
  const showDone =
    !anyVisibleRunning && (calls.length > 0 || !!reasoning);

  if (!reasoning && calls.length === 0) return null;

  return (
    <div className="ml-2 space-y-3 border-l border-border/40 pl-4 py-1">
      {reasoning && (
        <SimpleDetailRow icon={ClockIcon}>
          <ClampedReasoning text={reasoning} />
        </SimpleDetailRow>
      )}
      {calls.map((tc) => {
        // The todo tool's value is the visible task list itself —
        // a generic "Updated todo list" row hides exactly what the
        // user wants to see. Reuse the rich TodoToolBlock from the
        // Expert renderer for this one case; everything else stays
        // on the condensed icon + prose row.
        if (tc.toolName === "todo") {
          return (
            <div key={tc.id} className="pr-1">
              <TodoToolBlock tc={tc} />
            </div>
          );
        }
        return (
          <SimpleDetailRow key={tc.id} icon={_toolRowIcon(tc.toolName)}>
            <span>{_toolRowLabel(tc)}</span>
          </SimpleDetailRow>
        );
      })}
      {showDone && (
        <SimpleDetailRow icon={CircleCheckIcon}>
          <span className="text-foreground">Done</span>
        </SimpleDetailRow>
      )}
    </div>
  );
}

export function IterationGroup({
  message,
  sessionId,
  artifactFallbacks,
  onFileSelect,
}: IterationGroupProps) {
  const summary = message.iterationSummary?.summary;
  const [open, setOpen] = useState(false);

  // ask_user_question owns its iteration's rendering in BOTH states: the
  // interactive widget while awaiting an answer (a shimmer label alone
  // gives the user nothing to act on and the session stalls), and the
  // read-only Q/A recap once answered (AskUserQuestionToolBlock renders
  // its locked view) — instead of collapsing to a generic tool row that
  // drops the question and the user's choice from the thread.
  const askCall = (message.toolCalls ?? []).find(
    (tc) => tc.toolName === "ask_user_question",
  );
  if (askCall) {
    return (
      <div className="px-1 py-0.5">
        <AskUserQuestionToolBlock tc={askCall} />
      </div>
    );
  }

  // 1. Live iteration: shimmer label only. "Live" means a tool is
  //    actively running, OR a text-only iteration is mid-stream.
  //    message.status === "streaming" alone is NOT a reliable signal —
  //    tool-using messages stay in "streaming" state across the entire
  //    rest of the turn until the next llm.response lands or the turn
  //    ends. Without this stricter check, completed iterations would
  //    stay in the shimmer state forever (user-reported bug).
  if (isIterationLive(message)) {
    const label = liveIterationLabel(message);
    return (
      <div className="py-0.5 text-sm">
        <Shimmer duration={3} spread={3} className="text-sm">
          {label}
        </Shimmer>
      </div>
    );
  }

  // 2. Complete: collapsible row. Use the LLM summary when present,
  //    else derive a short label from the tool calls so the row stays
  //    informative without the summary_model auxiliary.
  const label = summary ?? deriveIterationLabel(message);
  if (!label) {
    // Nothing to summarize and nothing to derive. Skip the row
    // entirely — the surrounding SimpleAssistantGroup will still
    // render the final-answer text and TurnSummaryCard.
    return null;
  }
  const labelTone = summary
    ? "text-muted-foreground italic"
    : "text-muted-foreground";
  return (
    <div className="space-y-1">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 rounded py-0.5 text-left text-sm hover:bg-muted/40"
      >
        <span className={cn("flex-1 truncate", labelTone)}>{label}</span>
        <ChevronDown
          className={cn(
            "size-3 shrink-0 text-foreground/60 transition-transform",
            open && "rotate-180",
          )}
          aria-hidden
        />
      </button>
      {open && (
        <IterationExpanded
          message={message}
          sessionId={sessionId}
          artifactFallbacks={artifactFallbacks}
          onFileSelect={onFileSelect}
        />
      )}
    </div>
  );
}

/**
 * Synthetic file-artifact derivation for Simple mode.
 *
 * Scans an assistant turn's messages for tool calls that produced a
 * named workspace file (``write_file`` / ``patch``) or a structured
 * artifact (``create_artifact``) and turns each unique output path
 * into an :type:`AgentChatTurnArtifactRef`. Used as a fallback when
 * the harness's turn summarizer either isn't configured or omitted
 * file deliverables from ``turn.summary``.
 *
 * Limitations: the candidate set is the same set the harness's
 * candidate-artifact collector uses, so files written indirectly
 * (e.g. via a terminal Python script) still won't surface here —
 * that gap belongs upstream in the harness, not in the SDK.
 */
function deriveFileArtifactsFromMessages(
  messages: ChatMessageType[],
): AgentChatTurnArtifactRef[] {
  // First pass: gather every terminal command issued in this turn so
  // we can detect files the agent ran itself (intermediate scripts).
  // Mirrors the harness-side executed_by_terminal heuristic.
  const terminalCommands: string[] = [];
  for (const msg of messages) {
    if (msg.role !== "assistant" || !msg.toolCalls) continue;
    for (const tc of msg.toolCalls) {
      if (tc.toolName !== "terminal") continue;
      const status = effectiveStatus(tc);
      if (status === "error" || status === "cancelled") continue;
      const args = parseArgs<Record<string, unknown>>(tc.args);
      const cmd = typeof args?.command === "string" ? args.command : "";
      if (cmd) terminalCommands.push(cmd);
    }
  }

  const seen = new Set<string>();
  const out: AgentChatTurnArtifactRef[] = [];
  for (const msg of messages) {
    // Structured artifacts are derived from their ``artifact.created`` /
    // ``artifact.updated`` system markers, not the ``create_artifact``
    // tool call: the marker carries the ``artifact_id`` that
    // TurnSummaryCard's resolveArtifactRef matches on (the tool call only
    // knows the name, which would degrade the card to plain text). The
    // marker is also folded into the same group the summary renders in.
    if (msg.role === "system") {
      if (msg.systemKind !== "artifact") continue;
      const { artifactId, name } = unpackArtifactMeta(msg.systemMeta, msg.content);
      if (!artifactId || seen.has(`artifact:${artifactId}`)) continue;
      seen.add(`artifact:${artifactId}`);
      out.push({ kind: "artifact", label: name, ref: artifactId });
      continue;
    }
    if (msg.role !== "assistant" || !msg.toolCalls) continue;
    for (const tc of msg.toolCalls) {
      const status = effectiveStatus(tc);
      if (status === "error" || status === "cancelled") continue;
      const args = parseArgs<Record<string, unknown>>(tc.args);
      if (!args) continue;
      if (tc.toolName === "write_file" || tc.toolName === "patch") {
        const path = typeof args.path === "string"
          ? args.path
          : typeof args.file_path === "string"
            ? args.file_path
            : "";
        if (!path || seen.has(`file:${path}`)) continue;
        // Skip files the agent later ran as a script — these are
        // almost always scaffolding (chart generators, etc.), not
        // deliverables. The LLM-driven harness summarizer applies
        // the same rule via the executed_by_terminal annotation.
        const executed = terminalCommands.some((cmd) => cmd.includes(path));
        if (executed) continue;
        seen.add(`file:${path}`);
        out.push({
          kind: "file",
          label: path.split("/").pop() || path,
          ref: path,
        });
        continue;
      }
    }
  }
  return out;
}

// ── Simple-mode AssistantGroup ───────────────────────────────────────
//
// Renders an assistant turn as: per-iteration IterationGroup rows (one
// per assistant message), the final-answer text, a TurnSummaryCard
// when one is attached, and TurnFeedback on the final answer. System
// messages threaded into the group (skill_invoked / artifact_created)
// keep their existing markers so we don't lose their visibility in
// Simple mode.

function SimpleAssistantGroup({
  messages,
  isRunning,
  sessionId,
  artifactFallbacks,
  onFileSelect,
  onRetry,
  hideTurnSummary = false,
}: {
  messages: ChatMessageType[];
  isRunning: boolean;
  sessionId: string | null;
  artifactFallbacks: Record<string, string>;
  onFileSelect?: (path: string) => void;
  onRetry?: () => Promise<void>;
  hideTurnSummary?: boolean;
}) {
  const assistantMessages = messages.filter((m) => m.role === "assistant");
  const tail = assistantMessages[assistantMessages.length - 1];

  // The final assistant text is the tail message's content when that
  // message has no tool calls (the harness emits tools and text in
  // separate llm.response events, so the closing iteration is
  // text-only). Streaming tail with no content yet shows the running
  // shimmer instead.
  const tailHasTools = !!(tail?.toolCalls && tail.toolCalls.length > 0);
  const tailIsTextOnly = !!tail && !tailHasTools;
  const finalText = tailIsTextOnly && tail!.content ? tail!.content : "";

  const showErrorInfo =
    !!tail && tail.status === "error" && !!tail.errorInfo;

  // Synthetic-artifact fallback so file deliverables still surface
  // even when the harness summarizer is missing or didn't include
  // them in turn.summary. We scan every assistant message in the
  // group for write_file / patch / create_artifact tool calls and
  // promote each unique output path to a file artifact. The real
  // turn.summary always wins when present and non-empty.
  //
  // Gated on turn completion: the fallback would otherwise pop into
  // view as soon as the agent's first write_file lands, well before
  // the user-facing turn is done. We only synthesize once the tail
  // iteration has stopped streaming so the summary card matches what
  // the harness would have emitted at turn end.
  const turnComplete = !isRunning && tail?.status === "complete";
  // Honour the model's own ``summary="show|hide"`` declaration in the
  // tail iteration's <next_action> footer.  Default is "hide" so a
  // missing/malformed declaration suppresses the card — model has to
  // opt IN to the heavier UI affordance.  Artifact-derived summaries
  // still render even when hidden, because file output is a strong
  // signal the user wants the artifact tray visible.
  const summaryPref =
    finalText !== null
      ? stripAndParseNextAction(finalText).action?.summary ?? "hide"
      : "hide";
  const effectiveTurnSummary = (() => {
    const fromHarness = tail?.turnSummary;
    if (fromHarness && fromHarness.artifacts.length > 0) {
      return fromHarness;
    }
    // Synthetic file-artifact fallback surfaces file deliverables even
    // when the model opted to hide the recap (``summaryPref === "hide"``):
    // a download card is a strong, low-noise signal the user wants the
    // artifact tray. The recap *text* still honours the preference, so a
    // hidden turn carries the cards without the prose summary. Gated on
    // turn completion so it matches what the harness would emit at end.
    if (turnComplete) {
      const derived = deriveFileArtifactsFromMessages(messages);
      if (derived.length > 0) {
        return {
          turnId: fromHarness?.turnId ?? "",
          recap: summaryPref === "show" ? (fromHarness?.recap ?? "") : "",
          artifacts: derived,
        };
      }
    }
    // No artifacts to surface — honour the hide preference for the
    // recap-only card.
    if (summaryPref === "hide") return null;
    return fromHarness;
  })();

  return (
    <Message from="assistant">
      <MessageContent>
        <div className="space-y-2">
          {messages.map((message) => {
            if (message.role === "system") {
              // Simple mode hides skill_invoked markers; artifact and
              // error markers still render via OrphanSystemMarker.
              if (message.systemKind === "skill_invoked") return null;
              return (
                <OrphanSystemMarker
                  key={message.id}
                  message={message}
                  sessionId={sessionId}
                  onRetry={undefined}
                />
              );
            }
            // The tail text-only iteration is rendered as finalText
            // below — don't also render an IterationGroup row for it,
            // or its mid-stream "Thinking…" shimmer (and post-stream
            // "Thought through the problem" label) will sit above the
            // same text the user sees in finalText.
            //
            // BUT only skip once finalText is non-empty.  During the
            // initial thinking phase (reasoning streaming, no content
            // yet) finalText is "" and SimpleFinalAnswer won't render
            // anything either — letting IterationGroup through ensures
            // the live "Thinking…" shimmer is visible instead of a
            // blank gap.
            if (message === tail && tailIsTextOnly && finalText) return null;
            // Pull the narration line up as a sibling of the iteration
            // card so it reads as "agent voice at root level" instead
            // of buried inside the collapsible body.  Hidden when the
            // assistant message had no narration to surface (typed
            // ``done`` footer, no prose preamble, or empty content).
            const rawContent = (message.content ?? "").trim();
            const { action, inferredNarration, cleaned } =
              stripAndParseNextAction(rawContent);
            const narrationLine = action
              ? action.body.trim().toLowerCase() === "done"
                ? null
                : action.body
              : inferredNarration;
            // ``ask_user_question`` content is the full message the user
            // must act on (the proposed design behind the widget), so
            // render the whole body — collapsing it to a narration line
            // would hide what they're approving.
            const askBody = hasUserFacingAskContent(message) ? cleaned : null;
            return (
              <div key={message.id} className="space-y-2">
                {askBody ? (
                  <MessageResponse>{askBody}</MessageResponse>
                ) : narrationLine ? (
                  <p className="text-sm text-foreground">
                    {narrationLine}
                  </p>
                ) : null}
                <IterationGroup
                  message={message}
                  sessionId={sessionId}
                  artifactFallbacks={artifactFallbacks}
                  onFileSelect={onFileSelect}
                />
              </div>
            );
          })}
        </div>
        {finalText && (
          <SimpleFinalAnswer
            text={finalText}
            isStreaming={tail?.status === "streaming"}
            tail={tail}
          />
        )}
        {effectiveTurnSummary && !hideTurnSummary && (
          <TurnSummaryCard
            summary={effectiveTurnSummary}
            sessionId={sessionId}
            messages={messages}
            onFileSelect={onFileSelect}
          />
        )}
        {showErrorInfo && (
          <div className="mt-3">
            <ErrorMessage errorInfo={tail!.errorInfo!} onRetry={onRetry} />
          </div>
        )}
      </MessageContent>
    </Message>
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
  viewMode = "simple",
  hideTurnSummary = false,
}: {
  messages: ChatMessageType[];
  lastGlobalIndex: number;
  totalMessages: number;
  isRunning: boolean;
  sessionId: string | null;
  artifactFallbacks: Record<string, string>;
  onFileSelect?: (path: string) => void;
  onRetry?: () => Promise<void>;
  viewMode?: "simple" | "expert";
  hideTurnSummary?: boolean;
}) {
  if (viewMode === "simple") {
    return (
      <SimpleAssistantGroup
        messages={messages}
        isRunning={isRunning}
        sessionId={sessionId}
        artifactFallbacks={artifactFallbacks}
        onFileSelect={onFileSelect}
        onRetry={onRetry}
        hideTurnSummary={hideTurnSummary}
      />
    );
  }

  let entries: TimelineEntry[] = [];
  for (let i = 0; i < messages.length; i++) {
    const isLast = i === messages.length - 1
      && lastGlobalIndex === totalMessages - 1;
    entries.push(...messageToEntries(messages[i], isLast, artifactFallbacks));
  }
  entries = groupBrowserActivityEntries(entries);
  entries = groupWebSearchEntries(entries);

  // Mark only the last answer text entry in this assistant group as the
  // final user-facing answer (the thumbs control). A text-only message
  // contributes one text entry; an ask_user_question turn also emits a
  // text entry (its full body), but that body is a question prompt, not
  // the turn's answer, so it must never receive the feedback control.
  // A group can merge several messages; we want a single thumbs control
  // per visual turn.
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    if (entry?.kind === "text" && !hasUserFacingAskContent(entry.msg)) {
      entries[i] = { ...entry, isFinalTurnText: true };
      break;
    }
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
        <span className="text-[10px] text-foreground/60">
          attempt {indicator.attempt}
        </span>
        {indicator.detail && (
          open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />
        )}
      </button>
      {open && indicator.detail && (
        <pre className="mt-1 overflow-x-auto rounded-none bg-background p-2 font-mono text-[11px] whitespace-pre-wrap wrap-break-word text-foreground/60">
          {indicator.detail}
        </pre>
      )}
    </div>
  );
}

function WorkingOnItIndicator() {
  return (
    <Message from="assistant">
      <MessageContent>
        <Shimmer duration={3} spread={3} className="text-sm">
          Working on it...
        </Shimmer>
      </MessageContent>
    </Message>
  );
}

function useDelayedRunningIndicator(isRunning: boolean): boolean {
  const [visible, setVisible] = useState(isRunning);

  useEffect(() => {
    if (!isRunning) {
      setVisible(false);
      return;
    }
    if (visible) return;

    const timeout = window.setTimeout(() => {
      setVisible(true);
    }, WORKING_ON_IT_DELAY_MS);

    return () => {
      window.clearTimeout(timeout);
    };
  }, [isRunning, visible]);

  return visible;
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
  onComposerError,
  showBrowser = false,
  onToggleBrowser,
  showWorkspace = false,
  onToggleWorkspace,
  canShowBrowser = false,
  canShowWorkspace = false,
  viewMode = "simple",
  onViewModeChange,
  deepResearchEnabled = false,
  researchSources = [],
  hideTurnSummary = false,
}: ChatThreadProps) {
  const groups = useMemo(() => groupMessages(messages), [messages]);
  const awaitingInput = useMemo(() => isAwaitingUserInput(messages), [messages]);
  // Suppress the running shimmer while parked on a pending
  // ask_user_question — the agent is waiting on the user, not working.
  const showWorkingOnIt =
    useDelayedRunningIndicator(isRunning) && !awaitingInput;
  // Disable the composer while parked on a pending ask_user_question:
  // the user answers via the question widget, not the composer. Left
  // active it would show Stop (isRunning stays true) and abort the
  // session if the user typed an answer and pressed Enter.
  const composerDisabled = disabled || awaitingInput;
  const composerDisabledReason = awaitingInput
    ? "Answer the question above to continue."
    : disabledReason;

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
    <div className="flex flex-1 flex-col overflow-hidden bg-background text-base">
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
                      onFileSelect={onFileSelect}
                    />
                  );
                }

                if (group.role === "system") {
                  const msg = group.messages[0];
                  // Simple mode hides skill_invoked markers entirely.
                  if (
                    viewMode === "simple"
                    && msg.systemKind === "skill_invoked"
                  ) {
                    return null;
                  }
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
                    viewMode={viewMode}
                    hideTurnSummary={hideTurnSummary}
                  />
                );
              })}
              {showWorkingOnIt && <WorkingOnItIndicator />}
            </>
          )}
        </ConversationContent>
        <ConversationScrollButton />
      </Conversation>

      <div className="mx-auto w-full max-w-4xl px-3 sm:px-6 pb-3 sm:pb-5 pt-3">
        {retryIndicator && (
          <div className="mb-2">
            <RetryBanner indicator={retryIndicator} />
          </div>
        )}
        {researchSources.length > 0 && (
          <div className="mb-2">
            <ResearchSourcesPanel sources={researchSources} />
          </div>
        )}
        <ChatComposer
          onSend={onSend}
          onStop={onStop}
          isRunning={isRunning}
          disabled={composerDisabled}
          disabledReason={composerDisabledReason}
          tokenUsage={tokenUsage}
          onComposerError={onComposerError}
          showBrowser={showBrowser}
          onToggleBrowser={onToggleBrowser}
          showWorkspace={showWorkspace}
          onToggleWorkspace={onToggleWorkspace}
          canShowBrowser={canShowBrowser}
          canShowWorkspace={canShowWorkspace}
          viewMode={viewMode}
          onViewModeChange={onViewModeChange}
          deepResearchEnabled={deepResearchEnabled}
        />
      </div>
    </div>
  );
}

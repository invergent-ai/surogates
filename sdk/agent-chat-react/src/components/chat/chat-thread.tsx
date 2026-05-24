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
import { BrowserActivityGroup } from "../browser/browser-activity-group";
import { ToolCallBlock } from "./tool-call-block";
import { WebSearchGroupBlock } from "./tools/web-search-tool";
import { parseTerminalResult } from "./tools/terminal-tool";
import { statusColorClass, effectiveStatus, toolErrorSummary, parseArgs } from "./tools/shared";
import { ChatMessage } from "./chat-message";
import { ChatComposer } from "./chat-composer";
import { TurnFeedback } from "./turn-feedback";
import { useSmoothStream } from "./use-smooth-stream";
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
import { useState } from "react";
import type {
  AgentChatImageAttachment,
  AgentChatPendingAttachment,
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
      <div className="my-2 flex items-center gap-2 px-4 text-xs text-foreground/60 ">
        <span className="size-2 rounded-full bg-emerald-500" />
        <span>
          <span className="font-semibold text-foreground">Skill</span>
          <span className="text-foreground/60 truncate">{skill}</span>
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

function TextEntry({
  entry,
  step,
}: {
  entry: Extract<TimelineEntry, { kind: "text" }>;
  step: number;
}) {
  const content = useSmoothStream(entry.content, entry.isStreaming);
  return (
    <TimelineItem step={step}>
      <TimelineHeader>
        <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
        <TimelineIndicator className="size-2 border-none bg-foreground/40" />
      </TimelineHeader>
      <TimelineContent>
        <MessageResponse>{content}</MessageResponse>
        {entry.isFinalTurnText && entry.msg.status === "complete" && (
          <TurnFeedback msg={entry.msg} />
        )}
      </TimelineContent>
    </TimelineItem>
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
    case "web_extract":
      return stringArg("url");
    case "skill_view":
    case "skill_manage":
      return stringArg("name") ?? stringArg("skill");
    case "create_artifact":
    case "consult_expert":
    case "delegate_task":
      return stringArg("name") ?? stringArg("task") ?? stringArg("title");
    case "memory":
      return stringArg("action") ?? stringArg("key");
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
  "process",
  "todo",
  "memory",
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
      {calls.map((tc) => (
        <SimpleDetailRow key={tc.id} icon={_toolRowIcon(tc.toolName)}>
          <span>{_toolRowLabel(tc)}</span>
        </SimpleDetailRow>
      ))}
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
      <div className="px-1 py-0.5 text-sm">
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
    ? "text-foreground/80"
    : "text-foreground/50";
  return (
    <div className="space-y-1">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-sm hover:bg-muted/40"
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
  const assistantMessages = messages.filter((m) => m.role === "assistant");
  const tail = assistantMessages[assistantMessages.length - 1];

  // The final assistant text is the tail message's content when that
  // message has no tool calls (the harness emits tools and text in
  // separate llm.response events, so the closing iteration is
  // text-only). Streaming tail with no content yet shows the running
  // shimmer instead.
  const tailHasTools = !!(tail?.toolCalls && tail.toolCalls.length > 0);
  const finalText = tail && !tailHasTools && tail.content
    ? tail.content
    : "";

  // A tail iteration that's mid-stream and has no IterationSummary yet
  // surfaces via the IterationGroup's own placeholders. But if the
  // group's tail is the user message (no assistant message at all yet),
  // we still need a Working-on-it shimmer — same defensive layer the
  // Expert path appends.
  const isTailGroup = lastGlobalIndex === totalMessages - 1;
  const showThinkingShim =
    isTailGroup
    && isRunning
    && !tail
    && messages.some((m) => m.role !== "assistant");

  const showErrorInfo =
    !!tail && tail.status === "error" && !!tail.errorInfo;

  return (
    <Message from="assistant">
      <MessageContent>
        <div className="space-y-2">
          {messages.map((message) => {
            if (message.role === "system") {
              // Reuse the existing OrphanSystemMarker — it renders the
              // skill_invoked dot, artifact block, and error messages
              // the same way the orphan path does.
              return (
                <OrphanSystemMarker
                  key={message.id}
                  message={message}
                  sessionId={sessionId}
                  onRetry={undefined}
                />
              );
            }
            return (
              <IterationGroup
                key={message.id}
                message={message}
                sessionId={sessionId}
                artifactFallbacks={artifactFallbacks}
                onFileSelect={onFileSelect}
              />
            );
          })}
          {showThinkingShim && (
            <Shimmer duration={3} spread={3} className="text-sm">
              Working on it...
            </Shimmer>
          )}
        </div>
        {finalText && (
          <div className="mt-3">
            <MessageResponse>{finalText}</MessageResponse>
            {tail?.status === "complete" && <TurnFeedback msg={tail} />}
          </div>
        )}
        {tail?.turnSummary && (
          <TurnSummaryCard
            summary={tail.turnSummary}
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
}) {
  if (viewMode === "simple") {
    return (
      <SimpleAssistantGroup
        messages={messages}
        lastGlobalIndex={lastGlobalIndex}
        totalMessages={totalMessages}
        isRunning={isRunning}
        sessionId={sessionId}
        artifactFallbacks={artifactFallbacks}
        onFileSelect={onFileSelect}
        onRetry={onRetry}
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

  // Mark only the last text entry in this assistant group as the
  // final user-facing answer. Each constituent message can contribute
  // at most one text entry (messageToEntries only pushes text when
  // there are no tool calls), but a group can merge several messages;
  // we only want a single thumbs control per visual turn.
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    if (entry?.kind === "text") {
      entries[i] = { ...entry, isFinalTurnText: true };
      break;
    }
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
                    viewMode={viewMode}
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

      <div className="mx-auto w-full max-w-4xl px-3 sm:px-6 pb-3 sm:pb-5 pt-3">
        {retryIndicator && (
          <div className="mb-2">
            <RetryBanner indicator={retryIndicator} />
          </div>
        )}
        {disabled && disabledReason ? (
          <div
            className="rounded border border-line bg-muted/40 px-3 py-2 text-sm text-foreground/60"
            role="status"
          >
            {disabledReason}
          </div>
        ) : (
          <ChatComposer
            onSend={onSend}
            onStop={onStop}
            isRunning={isRunning}
            disabled={disabled}
            disabledReason={disabledReason}
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
          />
        )}
      </div>
    </div>
  );
}

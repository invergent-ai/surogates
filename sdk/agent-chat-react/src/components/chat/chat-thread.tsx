// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Custom chat thread — uses ai-elements Conversation + Message
// with a compact, Claude Code-inspired layout.
//
import { type ReactNode, useEffect, useMemo, useState } from "react";
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
} from "@invergent/ui-sdk/timeline";
import { OrbShimmerLabel } from "../ai-elements/orb-label";
import {
  deriveOrbActivity,
  messageOrbState,
  type OrbActivity,
} from "../../runtime/orb-state";
import {
  SIMPLE_MODE_ORB_OPTIONS,
  isHiddenSimpleTool,
} from "../../runtime/simple-mode";
import { BrowserActivityGroup } from "../browser/browser-activity-group";
import { ToolCallBlock } from "./tool-call-block";
import { CodeRunToolBlock } from "./tools/code-run-tool";
import { WebSearchGroupBlock } from "./tools/web-search-tool";
import { TodoToolBlock } from "./tools/todo-tool";
import {
  AskUserQuestionToolBlock,
  conversationalAskAcceptsFreeText,
  hasChoices,
  isConversationalAsk,
} from "./tools/ask-user-question-tool";
import { askQuestionsOf, isAsk, isLiveAsk } from "./tools/ask-questions";
import { parseTerminalResult } from "./tools/terminal-tool";
import {
  statusColorClass,
  effectiveStatus,
  toolErrorSummary,
  parseArgs,
  readableOutput,
  toolFailure,
  truncate,
} from "./tools/shared";
import {
  cancelledToolLabel,
  deriveSingleToolLabel,
  extractToolDetail,
  skillViewLabel,
  toolRowLabel,
} from "./simple-labels";
import { ChatMessage } from "./chat-message";
import { renderInlineMarkdown } from "./inline-markdown";
import { ChatComposer } from "./chat-composer";
import type { ComposerTrayProps } from "./composer-tray";
import { IntegrationsBand } from "../connections/integrations-band";
import { useAgentChatAdapterContext } from "../../adapter-context";
import { ResearchSourcesPanel } from "../research/research-sources-panel";
import { TurnFeedback } from "./turn-feedback";
import { useSmoothStream } from "./use-smooth-stream";
import { reasoningTokenLabel } from "../../runtime/reasoning-tokens";
import { stripAndParseNextAction } from "../../lib/next-action";
import { ArtifactBlock } from "./artifacts/artifact-block";
import { ErrorMessage } from "./error-message";
import { TurnDownloads } from "./turn-downloads";
import { ThreadCards } from "./thread-cards";
import { cn } from "../../lib/utils";
import { Alert, AlertAction, AlertDescription, AlertTitle } from "@invergent/ui-sdk/alert";
import { Button } from "@invergent/ui-sdk/button";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  LaptopIcon,
  Loader2Icon,
} from "lucide-react";
import type {
  AgentChatDeviceWaitState,
  AgentChatImageAttachment,
  AgentChatPendingAttachment,
  AgentChatResearchSource,
  AgentChatTurnArtifactRef,
  AgentChatViewMode,
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
  /**
   * Resources this session has produced, offered as tabs at the top of the
   * composer card. Forwarded as-is to ChatComposer.
   */
  paneCards?: ComposerTrayProps;

  browserProfileId?: string | null;
  onSelectBrowserProfile?: (id: string | null) => void;
  browserProfilesEnabled?: boolean;
  browserProfileLocked?: boolean;
  // Simple/Expert view-mode toggle — also threaded into AssistantGroup
  // in B9 to gate the actual render path. When omitted, the composer
  // hides the toggle and the thread defaults to Simple internally.
  viewMode?: "simple" | "expert";
  onViewModeChange?: (mode: AgentChatViewMode) => void;

  // When true, the composer's slash menu includes ``/deep-research``.
  // Forwarded as-is to ChatComposer.
  deepResearchEnabled?: boolean;
  // When true, the composer's slash menu includes ``/auto-research``
  // (research missions / Arbor). Forwarded as-is to ChatComposer.
  researchEnabled?: boolean;
  // When true, the composer's slash menu includes the ``/code`` commands.
  // Forwarded as-is to ChatComposer.
  codeAgentsEnabled?: boolean;
  whiteboardEnabled?: boolean;
  // Slash-command capability group. Forwarded as-is to ChatComposer:
  // these gate the always-on lightweight builtins (default shown).
  loopsEnabled?: boolean;
  missionsEnabled?: boolean;
  goalsEnabled?: boolean;
  compressEnabled?: boolean;
  // The composer's Commands and Skills menus. Forwarded as-is to
  // ChatComposer (default shown).
  commandsEnabled?: boolean;
  skillsEnabled?: boolean;
  // Adding files to a message. Forwarded as-is to ChatComposer (default shown).
  attachmentsEnabled?: boolean;

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

  // Composio integrations band, rendered under the composer. The band
  // opens the host's Integrations route via ``onOpenIntegrations``;
  // ``agentId`` scopes the connection lookup. Both omitted -> no band.
  agentId?: string;
  onOpenIntegrations?: () => void;
  // A chat on a folder of the user's computer whose work waits for that computer.
  deviceWait?: AgentChatDeviceWaitState | null;
  // What the host says under the composer, as where a new chat will work.
  composerFooter?: ReactNode;
  // No composer, footer or integrations band: the transcript fills the chat. The area under it
  // still shows the device-wait line, the retry banner and the research sources. Every reply
  // shows whole, at the final answer's size, never cut to its first sentence.
  hideComposer?: boolean;
}

// ── Timeline item types ──────────────────────────────────────────────

type TimelineEntry =
  | { kind: "reasoning"; key: string; reasoning: string; isStreaming: boolean }
  | {
      kind: "tool";
      key: string;
      tc: ToolCallInfo;
      resolvedArtifactName?: string;
      assistantContent?: string;
    }
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
  | { kind: "thread_card"; key: string; message: ChatMessageType }
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

// The transcript and composer column (every max-w-4xl inside the thread):
// Claude's medium measure, 960px of text inside 32px gutters. A host's own
// transcript-width setting overrides it — its rule outranks this utility.
const TRANSCRIPT_WIDTH = "[--container-4xl:64rem]";

/**
 * ``ask_user_question`` is the one tool whose accompanying assistant
 * ``content`` is a full user-facing message body (e.g. a proposed
 * design the user must approve) rather than the throwaway preamble
 * other tool calls emit.  Such turns must render their body in full —
 * collapsing it to a one-line narration drops the very content the user
 * is being asked to act on.  A call the tool refuses (or would) asks
 * nothing, so its body is an ordinary preamble.
 */
function hasUserFacingAskContent(msg: ChatMessageType): boolean {
  return !!msg.toolCalls?.some(isAsk);
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
 * means the user moved on, so it's no longer awaiting.  A malformed
 * call is not awaiting anything: the tool is about to refuse it.
 */
function pendingAskCall(messages: ChatMessageType[]): ToolCallInfo | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === "user") return null;
    if (m.role !== "assistant") continue; // skip trailing system markers
    return m.toolCalls?.find(isLiveAsk) ?? null;
  }
  return null;
}

/**
 * How the composer should behave while a question is pending.
 *
 * ``null`` when nothing is pending.  Otherwise the composer stays open
 * unless the agent offered a closed menu: the server turns a typed
 * message into the answer for whatever question is open (see
 * ``try_resolve_text_answer``), so for a conversational ask the
 * composer IS the answer field — locking it would strand the user with
 * a question they can see and cannot answer in the obvious way.
 *
 * A multi-question batch keeps the composer locked: its answers are
 * collected per question in the widget, and a single typed message
 * would be recorded as the answer to every one of them.
 */
function askComposerPolicy(
  pending: ToolCallInfo | null,
): { locked: boolean; reason?: string; placeholder?: string } | null {
  if (!pending) return null;
  // ``pending`` is a live ask, so it has at least one valid question.
  const questions = askQuestionsOf(pending);
  if (!isConversationalAsk(questions)) {
    return { locked: true, reason: "Answer the questions above to continue." };
  }

  const question = questions[0]!;
  if (!conversationalAskAcceptsFreeText(question)) {
    return {
      locked: true,
      reason: "Pick one of the options above to continue.",
    };
  }
  return {
    locked: false,
    placeholder: hasChoices(question)
      ? "Pick one above, or type your answer…"
      : "Type your answer…",
  };
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
    if (msg.systemKind === "worker" || msg.systemKind === "thread_proposal") {
      return [{ kind: "thread_card", key: msg.id, message: msg }];
    }
    if (
      msg.systemKind === "browser_marker"
      || msg.systemKind === "browser_marker_warning"
      || msg.systemKind === "notice"
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
      entries.push({
        kind: "tool",
        key: tc.id,
        tc,
        // Only ask_user_question reads this, to suppress a prompt the
        // agent already spelled out in its message body.
        assistantContent:
          tc.toolName === "ask_user_question" ? msg.content : undefined,
      });
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
 * calls -- not as a floating row outside the timeline.  Consecutive
 * system markers no assistant turn follows (yet) share one ``system``
 * group, which the thread draws as a run.
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
    const last = pendingSystem[pendingSystem.length - 1];
    if (last) {
      groups.push({
        role: "system",
        messages: pendingSystem.map((p) => p.msg),
        lastGlobalIndex: last.index,
      });
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

    // user — drain any orphan system markers (no assistant followed)
    // into one group, then push the user turn as its own group.
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
// produced an assistant turn to fold it into. In a run of markers
// (``inRun``) the run spaces its rows, so a row drops its own margin.

function OrphanSystemMarker({
  message,
  sessionId,
  onRetry,
  inRun = false,
}: {
  message: ChatMessageType;
  sessionId: string | null;
  onRetry?: () => Promise<void>;
  inRun?: boolean;
}) {
  const margin = inRun ? undefined : "my-2";
  if (message.systemKind === "skill_invoked") {
    const skill = (message.systemMeta?.skill as string) ?? message.content;
    return (
      <div className={cn(margin, "flex items-center gap-2 px-4 text-xs text-foreground/60")}>
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
    || message.systemKind === "notice"
  ) {
    const warning = message.systemKind === "browser_marker_warning";
    return (
      <div
        className={cn(
          margin,
          "flex items-center gap-2 px-4 text-xs",
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
      <div className={cn(margin, "mx-auto w-full max-w-4xl")}>
        <ErrorMessage errorInfo={message.errorInfo} onRetry={onRetry} />
      </div>
    );
  }

  if (message.systemKind === "worker" || message.systemKind === "thread_proposal") {
    return (
      <div className="mx-auto w-full max-w-4xl px-4">
        <ThreadCards message={message} />
      </div>
    );
  }

  return null;
}

// ── Cancelled tool row (parallel-batch sibling-error cancellation) ──


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
                assistantContent={entry.assistantContent}
                // The timeline only renders in Expert mode; Simple goes
                // through SimpleAssistantGroup / IterationGroup.
                viewMode="expert"
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

  if (entry.kind === "thread_card") {
    return (
      <TimelineItem step={step}>
        <TimelineHeader>
          <TimelineSeparator style={{ backgroundColor: "var(--color-border)" }} />
          <TimelineIndicator className="size-2 border-none bg-foreground/30" />
        </TimelineHeader>
        <TimelineContent>
          <ThreadCards message={entry.message} />
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
        <TimelineIndicator className="size-2 border-none bg-primary" />
      </TimelineHeader>
      <TimelineContent>
        <OrbShimmerLabel state="working" label="Working on it..." className="py-0.5" />
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
        <p>{renderInlineMarkdown(line)}</p>
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
        <LoopResultAffordance message={entry.msg} />
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
  /** Only the current iteration may display a live status in the thread. */
  isActive?: boolean;
}

/**
 * The subset of an iteration's tool calls that should surface in
 * Simple mode: skips internal/infrastructure tools and any call
 * that errored or was cancelled. Used by every Simple-mode
 * consumer so the filter has a single source of truth.
 */
function visibleToolCalls(message: ChatMessageType): ToolCallInfo[] {
  return (message.toolCalls ?? []).filter((tc) => {
    if (isHiddenSimpleTool(tc)) return false;
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

function hasInlineRunningIndicator(
  messages: ChatMessageType[],
  viewMode: "simple" | "expert",
): boolean {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message.role === "user") return false;
    if (message.role !== "assistant") continue;

    if (viewMode === "simple") {
      // The question widget owns ask iterations. Text-only tails with
      // content render as the final answer instead of an iteration row.
      if (message.toolCalls?.some(isAsk)) {
        return false;
      }
      return isIterationLive(message)
        && (!!message.toolCalls?.length || !message.content || message.content === message.reasoning);
    }

    // Use the timeline's streaming rules, including its treatment of
    // trailing system markers and content that repeats the reasoning.
    return messageToEntries(message, i === messages.length - 1, {}).some(
      (entry) => entry.kind === "reasoning" && entry.isStreaming,
    );
  }
  return false;
}

/** A call still running, in the words its finished row will use. */
function runningCallLabel(tc: ToolCallInfo): string {
  if (tc.toolName === "skill_view") return `${skillViewLabel([tc])}…`;
  const name = cancelledToolLabel(tc.toolName);
  const detail = extractToolDetail(tc);
  return detail ? `Running ${name} · ${detail}…` : `Running ${name}…`;
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
    (tc) => tc.status === "running" && !isHiddenSimpleTool(tc),
  );
  if (running.length === 0) return reasoningTokenLabel(message, true);
  // Same wording as the finished row, so it does not rename itself.
  if (running.every((tc) => tc.toolName === "skill_view")) {
    return `${skillViewLabel(running)}…`;
  }
  if (running.length === 1) return runningCallLabel(running[0]!);
  const firstName = running[0]!.toolName;
  const allSame = running.every((tc) => tc.toolName === firstName);
  if (allSame) {
    return `Running ${cancelledToolLabel(firstName)} × ${running.length}…`;
  }
  return `Running ${running.length} tools…`;
}



// ponytail: a cap, not paging — the Expert view has the whole result.
const OUTPUT_MAX_CHARS = 20_000;

// Tools whose output is the agent's own instructions — a skill's text, the
// skill list — not a result anyone opens a row to check.
const NO_OUTPUT_TOOLS: ReadonlySet<string> = new Set(["skill_view", "skills_list"]);

/**
 * The calls that have something to show when their row opens: an output, a
 * failure, or a block of their own. Hidden tools count — a command's
 * terminal or browser call is exactly the detail a reader opens the row to
 * check. Skill reads never do.
 */
function callsWithOutput(calls: ToolCallInfo[]): ToolCallInfo[] {
  return calls.filter((tc) => {
    if (tc.cancelled || NO_OUTPUT_TOOLS.has(tc.toolName)) return false;
    if (tc.toolName === "todo" || tc.toolName === "code_run") return true;
    return effectiveStatus(tc) === "error" || readableOutput(tc.result) !== "";
  });
}

/** One row of a command list: what it says, and the calls it opens onto. */
interface Command {
  key: string;
  label: string;
  calls: ToolCallInfo[];
  /** The iteration it came from: a group is keyed and timed by its first. */
  message: ChatMessageType;
}

/**
 * The commands of a finished iteration. A summary speaks for all of it, so
 * it is one row opening onto every call. Without one, each call the user
 * sees is a row of its own — never a "Used 2 tools" bundle beside them.
 */
function iterationCommands(message: ChatMessageType): Command[] {
  const summary = message.iterationSummary?.summary;
  if (summary) {
    return [{
      key: message.id,
      label: summary,
      calls: callsWithOutput(message.toolCalls ?? []),
      message,
    }];
  }
  return visibleToolCalls(message).map((tc) => ({
    key: tc.id,
    label: deriveSingleToolLabel(tc),
    calls: callsWithOutput([tc]),
    message,
  }));
}

/** The running iteration's calls so far, a running one worded as running. */
function liveCommands(message: ChatMessageType): Command[] {
  return visibleToolCalls(message).map((tc) => ({
    key: tc.id,
    label: tc.status === "running" ? runningCallLabel(tc) : deriveSingleToolLabel(tc),
    calls: callsWithOutput([tc]),
    message,
  }));
}

function ToolCallOutput({ tc }: { tc: ToolCallInfo }) {
  // The todo list and a coding agent's run are their own output.
  if (tc.toolName === "todo") return <TodoToolBlock tc={tc} />;
  if (tc.toolName === "code_run") return <CodeRunToolBlock tc={tc} defaultOpen />;
  if (effectiveStatus(tc) === "error") {
    const { code, message } = toolFailure(tc.result);
    return (
      <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs">
        <p className="font-mono font-medium text-destructive">{code ?? "Failed"}</p>
        {message && (
          <p className="mt-1 whitespace-pre-wrap break-words text-foreground/80">
            {truncate(message, OUTPUT_MAX_CHARS)}
          </p>
        )}
      </div>
    );
  }
  return (
    <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/50 px-3 py-2 font-mono text-xs text-foreground/80">
      {truncate(readableOutput(tc.result), OUTPUT_MAX_CHARS)}
    </pre>
  );
}

/** What a command's calls returned, each named when there is more than one. */
function CommandOutput({ calls }: { calls: ToolCallInfo[] }) {
  return (
    <div className="space-y-3">
      {calls.map((tc) => (
        <div key={tc.id} className="space-y-1.5">
          {calls.length > 1 && (
            <p className="text-xs text-muted-foreground">{toolRowLabel(tc)}</p>
          )}
          <ToolCallOutput tc={tc} />
        </div>
      ))}
    </div>
  );
}

/**
 * How Simple mode shows an iteration. A "command" is a finished one with a
 * label and details to expand: the rows that adjacent runs are grouped from.
 */
type IterationKind = "ask" | "live" | "command" | "note" | "none";

function iterationKind(message: ChatMessageType, isActive: boolean): IterationKind {
  // ask_user_question owns its iteration's rendering in BOTH states: the
  // question (and any quick replies) while awaiting an answer — a shimmer
  // label alone gives the user nothing to act on and the session stalls —
  // and the question again once answered, so the exchange stays in the
  // thread instead of collapsing to a generic tool row that drops it.
  // A call the tool refuses (or would) is not a question, so it does
  // not take the iteration over.
  if ((message.toolCalls ?? []).some(isAsk)) {
    return "ask";
  }
  // "Live" means a tool is actively running, OR a text-only iteration is
  // mid-stream. message.status === "streaming" alone is NOT a reliable
  // signal — tool-using messages stay in "streaming" state across the
  // entire rest of the turn until the next llm.response lands or the turn
  // ends. Without this stricter check, completed iterations would stay in
  // the shimmer state forever (user-reported bug).
  if (isActive && isIterationLive(message)) return "live";
  // A summary, or a call the user sees, makes a command. A reasoning-only
  // iteration is a quiet line with nothing to open; with neither, no row at
  // all — the surrounding SimpleAssistantGroup still renders the answer.
  if (message.iterationSummary?.summary || visibleToolCalls(message).length > 0) {
    return "command";
  }
  return message.reasoning || message.reasoningTokens ? "note" : "none";
}

interface CommandRowProps {
  label: string;
  calls: ToolCallInfo[];
  /** A row of a group's box: padded to the box, which draws its edges. */
  boxed?: boolean;
}

/** One command: its description, opening onto what it returned. With
 *  nothing returned yet there is nothing to open, so no chevron. */
function CommandRow({ label, calls, boxed = false }: CommandRowProps) {
  const [open, setOpen] = useState(false);
  const rowClass = cn(
    "flex w-full items-center gap-1 text-left text-sm text-muted-foreground",
    boxed ? "py-2" : "py-0.5",
  );
  const text = <span className="min-w-0 truncate">{renderInlineMarkdown(label)}</span>;
  return (
    <div className={cn(boxed && "px-3")}>
      {calls.length === 0 ? (
        <div className={rowClass}>{text}</div>
      ) : (
        <button
          type="button"
          onClick={() => setOpen((prev) => !prev)}
          aria-expanded={open}
          className={cn(rowClass, "transition-colors hover:text-foreground")}
        >
          {text}
          <ChevronRight
            className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-90")}
            aria-hidden
          />
        </button>
      )}
      {open && calls.length > 0 && (
        <div className={boxed ? "pb-3" : "pt-1"}>
          <CommandOutput calls={calls} />
        </div>
      )}
    </div>
  );
}

/** Whole seconds since *since*, ticking; null without a start. */
function useElapsedSeconds(since: Date | undefined): number | null {
  const start = since?.getTime();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (start === undefined) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [start]);
  return start === undefined ? null : Math.max(0, Math.floor((now - start) / 1000));
}

function formatElapsed(seconds: number): string {
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

interface CommandGroupProps {
  /** Finished commands, oldest first. */
  commands: Command[];
  /** The iteration running at the end of the run, while the run goes on. */
  live?: ChatMessageType;
}

/**
 * Adjacent commands under one header. While the run goes on, the header is
 * the running iteration's label with its orb and a clock, and the box is
 * open so the work shows as it happens; once done it reads "Ran N commands"
 * and folds to that one line. A click on the header overrides both.
 */
function CommandGroup({ commands, live }: CommandGroupProps) {
  const [chosen, setChosen] = useState<boolean | null>(null);
  const open = chosen ?? live !== undefined;
  // ponytail: createdAt is when this client saw the event, so a reload
  // mid-run restarts the clock; needs the event's own timestamp to fix.
  const elapsed = useElapsedSeconds(live ? (commands[0]?.message ?? live).createdAt : undefined);
  const rows = live ? [...commands, ...liveCommands(live)] : commands;
  return (
    <div className="space-y-1.5">
      <button
        type="button"
        onClick={() => setChosen(!open)}
        aria-expanded={open}
        className="flex max-w-full items-center gap-2 py-0.5 text-left text-sm"
      >
        {live ? (
          <>
            <OrbShimmerLabel
              state={messageOrbState(live, SIMPLE_MODE_ORB_OPTIONS)}
              label={liveIterationLabel(live)}
              className="min-w-0"
            />
            {elapsed !== null && (
              <span className="shrink-0 tabular-nums text-muted-foreground">
                {formatElapsed(elapsed)}
              </span>
            )}
          </>
        ) : (
          <span className="text-foreground/90">Ran {commands.length} commands</span>
        )}
        <ChevronDown
          className={cn(
            "-ml-1 size-3.5 shrink-0 text-muted-foreground transition-transform",
            !open && "-rotate-90",
          )}
          aria-hidden
        />
      </button>
      {open && rows.length > 0 && (
        <div
          data-testid="command-group-rows"
          // Under the label, not the orb, while it runs.
          className={cn(
            "divide-y divide-border overflow-hidden rounded-lg border border-border",
            live && "ml-7",
          )}
        >
          {rows.map((row) => (
            <CommandRow key={row.key} label={row.label} calls={row.calls} boxed />
          ))}
        </div>
      )}
    </div>
  );
}

export function IterationGroup({
  message,
  isActive = true,
}: IterationGroupProps) {
  const kind = iterationKind(message, isActive);
  const askCall = (message.toolCalls ?? []).find(isAsk);
  if (kind === "ask" && askCall) {
    return (
      <div className="px-1 py-0.5">
        <AskUserQuestionToolBlock
          tc={askCall}
          assistantContent={message.content}
          viewMode="simple"
        />
      </div>
    );
  }
  if (kind === "live") {
    // Same hidden-tool policy as the label: a lone internal list_files
    // must not leak a "searching" orb next to the quiet "Thinking…".
    return (
      <OrbShimmerLabel
        state={messageOrbState(message, SIMPLE_MODE_ORB_OPTIONS)}
        label={liveIterationLabel(message)}
        className="py-0.5"
      />
    );
  }
  if (kind === "none") return null;
  if (kind === "note") {
    return (
      <p className="py-0.5 text-sm text-muted-foreground">
        {reasoningTokenLabel(message, false)}
      </p>
    );
  }
  const commands = iterationCommands(message);
  return commands.length === 1 ? (
    <CommandRow label={commands[0]!.label} calls={commands[0]!.calls} />
  ) : (
    <CommandGroup commands={commands} />
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
// per assistant message), the final-answer text, the turn's downloads
// when it delivered files, and TurnFeedback on the final answer. System
// messages threaded into the group (skill_invoked / artifact_created)
// keep their existing markers so we don't lose their visibility in
// Simple mode.

function LoopResultAffordance({ message }: { message?: ChatMessageType }) {
  if (!message?.loopResult) return null;
  const completedAt = message.loopResult.runCompletedAt
    ? new Date(message.loopResult.runCompletedAt)
    : null;
  const time =
    completedAt && !Number.isNaN(completedAt.getTime())
      ? completedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : "";
  return (
    <div className="text-xs text-muted-foreground/70">
      From loop{time ? ` · ${time}` : ""}
    </div>
  );
}

function SimpleAssistantGroup({
  messages,
  activeAssistantId,
  isRunning,
  sessionId,
  onRetry,
  hideTurnSummary = false,
  fullReplies = false,
}: {
  messages: ChatMessageType[];
  activeAssistantId?: string;
  isRunning: boolean;
  sessionId: string | null;
  onRetry?: () => Promise<void>;
  hideTurnSummary?: boolean;
  /** Every reply whole, at the final answer's size, never cut to a narration line. */
  fullReplies?: boolean;
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
  const finalText = tailIsTextOnly && tail!.content !== tail!.reasoning
    ? tail!.content
    : "";

  const showErrorInfo =
    !!tail && tail.status === "error" && !!tail.errorInfo;

  // The turn's downloads: the files the harness summarizer named, or failing
  // that, the ones the turn's own tool calls wrote. The fallback waits for
  // the turn to end, so it does not pop in at the agent's first write_file
  // and matches what the harness would have emitted at turn end.
  const turnComplete = !isRunning && tail?.status === "complete";
  const turnFiles = (() => {
    const named = (tail?.turnSummary?.artifacts ?? []).filter((a) => a.kind === "file");
    if (named.length > 0) return named;
    return turnComplete ? deriveFileArtifactsFromMessages(messages) : [];
  })();

  // Adjacent commands fold into one group: "Ran 5 commands" once done, the
  // running one's label with a clock while it goes. Anything said or shown
  // between two commands — narration, a marker, a question — ends the run.
  const rows: ReactNode[] = [];
  let run: Command[] = [];
  const endRun = (live?: ChatMessageType) => {
    // Keyed by its first iteration, so a run keeps its open state as the
    // running iteration finishes and its calls settle into rows.
    const key = (run[0]?.message ?? live)?.id;
    if (live && (run.length > 0 || liveCommands(live).length > 0)) {
      rows.push(<CommandGroup key={key} commands={run} live={live} />);
    } else {
      if (run.length > 1) {
        rows.push(<CommandGroup key={key} commands={run} />);
      } else if (run.length === 1) {
        // One command is a row of its own: a group of one says nothing a
        // row does not.
        rows.push(
          <CommandRow key={run[0]!.key} label={run[0]!.label} calls={run[0]!.calls} />,
        );
      }
      if (live) {
        rows.push(
          <IterationGroup
            key={live.id}
            message={live}
          />,
        );
      }
    }
    run = [];
  };
  for (const message of messages) {
    if (message.role === "system") {
      // Simple mode hides skill_invoked markers; artifact and error
      // markers still render via OrphanSystemMarker.
      if (message.systemKind === "skill_invoked") continue;
      endRun();
      rows.push(
        <OrphanSystemMarker
          key={message.id}
          message={message}
          sessionId={sessionId}
          onRetry={undefined}
        />,
      );
      continue;
    }
    // The tail text-only iteration is rendered as finalText below — don't
    // also render an iteration row for it, or its mid-stream "Thinking…"
    // shimmer (and post-stream "Thought through the problem" label) will
    // sit above the same text the user sees in finalText.
    //
    // BUT only skip once finalText is non-empty.  During the initial
    // thinking phase (reasoning streaming, no content yet) finalText is ""
    // and SimpleFinalAnswer won't render anything either — letting the
    // iteration through ensures the live "Thinking…" shimmer is visible
    // instead of a blank gap.
    if (message === tail && tailIsTextOnly && finalText) continue;
    // Pull the narration line up as a sibling of the iteration row so it
    // reads as "agent voice at root level" instead of buried inside the
    // collapsible body.  Hidden when the assistant message had no
    // narration to surface (typed ``done`` footer, no prose preamble, or
    // empty content).
    const rawContent = message.content === message.reasoning
      ? ""
      : (message.content ?? "").trim();
    const { action, inferredNarration, cleaned } =
      stripAndParseNextAction(rawContent);
    const narrationLine = action
      ? action.body.trim().toLowerCase() === "done"
        ? null
        : action.body
      : inferredNarration;
    // ``ask_user_question`` content is the full message the user must act
    // on (the proposed design behind the widget), so render the whole body
    // — collapsing it to a narration line would hide what they're
    // approving. A chat with ``fullReplies`` renders every reply whole.
    const wholeBody = fullReplies || hasUserFacingAskContent(message) ? cleaned : null;
    if (wholeBody || narrationLine) {
      endRun();
      rows.push(
        wholeBody ? (
          <MessageResponse key={`${message.id}:narration`}>{wholeBody}</MessageResponse>
        ) : (
          <p key={`${message.id}:narration`} className="text-sm text-foreground">
            {renderInlineMarkdown(narrationLine ?? "")}
          </p>
        ),
      );
    }
    const isActive = message.id === activeAssistantId;
    const kind = iterationKind(message, isActive);
    if (kind === "none") continue;
    if (kind === "command") {
      run.push(...iterationCommands(message));
      continue;
    }
    if (kind === "live") {
      endRun(message);
      continue;
    }
    endRun();
    rows.push(
      <IterationGroup
        key={message.id}
        message={message}
        isActive={isActive}
      />,
    );
  }
  endRun();

  return (
    <Message from="assistant">
      <MessageContent>
        <div className="space-y-2">{rows}</div>
        {finalText && (
          <div className="space-y-1">
            <LoopResultAffordance message={tail} />
            <SimpleFinalAnswer
              text={finalText}
              isStreaming={tail?.status === "streaming"}
              tail={tail}
            />
          </div>
        )}
        {turnFiles.length > 0 && sessionId && !hideTurnSummary && (
          <TurnDownloads sessionId={sessionId} files={turnFiles} />
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
  activeAssistantId,
  lastGlobalIndex,
  totalMessages,
  isRunning,
  sessionId,
  artifactFallbacks,
  onFileSelect,
  onRetry,
  viewMode = "simple",
  hideTurnSummary = false,
  fullReplies = false,
}: {
  messages: ChatMessageType[];
  activeAssistantId?: string;
  lastGlobalIndex: number;
  totalMessages: number;
  isRunning: boolean;
  sessionId: string | null;
  artifactFallbacks: Record<string, string>;
  onFileSelect?: (path: string) => void;
  onRetry?: () => Promise<void>;
  viewMode?: "simple" | "expert";
  hideTurnSummary?: boolean;
  fullReplies?: boolean;
}) {
  if (viewMode === "simple") {
    return (
      <SimpleAssistantGroup
        messages={messages}
        activeAssistantId={activeAssistantId}
        isRunning={isRunning}
        sessionId={sessionId}
        onRetry={onRetry}
        hideTurnSummary={hideTurnSummary}
        fullReplies={fullReplies}
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
// retrying after a provider error, with the raw detail behind a toggle.
function RetryBanner({ indicator }: { indicator: RetryIndicator }) {
  const [open, setOpen] = useState(false);
  return (
    <Alert variant="warning" role="status">
      <AlertTriangle />
      <AlertTitle className="flex min-w-0 items-center gap-2 font-medium">
        <span className="min-w-0 flex-1 truncate">{indicator.title}</span>
        <span className="shrink-0 text-[11.5px] font-normal opacity-70">
          attempt {indicator.attempt}
        </span>
      </AlertTitle>
      {indicator.detail && (
        <AlertAction>
          <Button
            variant="ghost"
            size="xs"
            onClick={() => setOpen((prev) => !prev)}
            aria-expanded={open}
          >
            {open ? "Hide details" : "Details"}
            {open ? <ChevronDown /> : <ChevronRight />}
          </Button>
        </AlertAction>
      )}
      {open && indicator.detail && (
        <AlertDescription>
          <pre className="mt-1 overflow-x-auto rounded-[6px] bg-background p-2 font-mono text-[11px] whitespace-pre-wrap wrap-break-word text-foreground/60">
            {indicator.detail}
          </pre>
        </AlertDescription>
      )}
    </Alert>
  );
}

function WorkingOnItIndicator({ activity }: { activity: OrbActivity }) {
  return (
    <Message from="assistant">
      <MessageContent>
        <OrbShimmerLabel state={activity.state} label={activity.label} />
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

// How long after the loaded history shows its height may still change, as markdown and code blocks render.
const HISTORY_SETTLE_MS = 1500;

/**
 * False while the history loads and for ``settleMs`` after it shows, true from then on.
 * A thread that never loaded a history starts true.
 */
export function useHistorySettled(isLoadingHistory: boolean, settleMs: number): boolean {
  const [settled, setSettled] = useState(!isLoadingHistory);

  useEffect(() => {
    if (isLoadingHistory) {
      setSettled(false);
      return;
    }
    const timeout = window.setTimeout(() => setSettled(true), settleMs);
    return () => window.clearTimeout(timeout);
  }, [isLoadingHistory, settleMs]);

  return settled;
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
  paneCards,
  browserProfileId,
  onSelectBrowserProfile,
  browserProfilesEnabled = false,
  browserProfileLocked = false,
  viewMode = "simple",
  onViewModeChange,
  deepResearchEnabled = false,
  researchEnabled = false,
  codeAgentsEnabled = false,
  whiteboardEnabled = false,
  loopsEnabled = true,
  missionsEnabled = true,
  goalsEnabled = true,
  compressEnabled = true,
  commandsEnabled = true,
  skillsEnabled = true,
  attachmentsEnabled = true,
  researchSources = [],
  hideTurnSummary = false,
  agentId,
  onOpenIntegrations,
  deviceWait = null,
  composerFooter,
  hideComposer = false,
}: ChatThreadProps) {
  const groups = useMemo(() => groupMessages(messages), [messages]);
  const activeAssistantId = useMemo(() => {
    if (!isRunning) return undefined;
    for (let i = messages.length - 1; i >= 0; i--) {
      const message = messages[i];
      if (message.role === "user") return undefined;
      if (message.role === "assistant") return message.id;
    }
    return undefined;
  }, [messages, isRunning]);
  // Split so the JSON parse inside the policy is keyed on the pending
  // call's args rather than the messages array, which is rebuilt on
  // every streamed token anywhere in the thread.
  const pendingAsk = useMemo(() => pendingAskCall(messages), [messages]);
  const askPolicy = useMemo(
    () => askComposerPolicy(pendingAsk),
    [pendingAsk?.id, pendingAsk?.args],
  );
  const awaitingInput = askPolicy !== null;
  const orbActivity = useMemo(
    () => {
      const activity = deriveOrbActivity(
        messages,
        viewMode === "simple" ? SIMPLE_MODE_ORB_OPTIONS : undefined,
      );
      if (viewMode === "simple" && activity.state === "solving") {
        const latest = [...messages].reverse().find((message) => message.role === "assistant");
        if (latest && !latest.toolCalls?.some((tc) => tc.status === "running")) {
          return { ...activity, label: reasoningTokenLabel(latest, true) };
        }
      }
      return activity;
    },
    [messages, viewMode],
  );
  const inlineRunningIndicator = useMemo(
    () => hasInlineRunningIndicator(messages, viewMode),
    [messages, viewMode],
  );
  // Keep the footer for gaps where no message already shows a live
  // status. A pending question also suppresses it while awaiting input.
  const showWorkingOnIt =
    useDelayedRunningIndicator(isRunning) && !awaitingInput && !inlineRunningIndicator;
  const historySettled = useHistorySettled(isLoadingHistory, HISTORY_SETTLE_MS);
  // A conversational ask leaves the composer open — it IS the answer
  // field, and ``awaitingAnswer`` keeps Enter from stopping the session
  // (isRunning stays true while the agent is parked). A multi-question
  // batch is collected in the widget instead, so its composer locks.
  const composerLocked = askPolicy?.locked ?? false;
  const composerDisabled = disabled || composerLocked;
  const composerDisabledReason = composerLocked
    ? askPolicy?.reason
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

  const composerArea = (
    <div className="mx-auto w-full max-w-4xl px-3 sm:px-6 pb-3 sm:pb-5 pt-3">
      {/* There before it speaks, so that a screen reader hears it when it does. */}
      <div
        role="status"
        data-testid="device-wait"
        className={deviceWait ? "mb-2 flex items-center gap-2 rounded-lg border border-line bg-card px-3 py-2 text-sm text-muted-foreground" : undefined}
      >
        {deviceWait && (
          <>
            <LaptopIcon className="size-4 shrink-0" aria-hidden="true" />
            {deviceWait.reason === "copying"
              ? `Making this thread's copy of ${deviceWait.folder || "its folder"}…`
              : `Waiting for ${deviceWait.deviceName || "your computer"}. The chat goes on once it is back online.`}
          </>
        )}
      </div>
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
      {!hideComposer && (
        <>
          <ChatComposer
            onSend={onSend}
            onStop={onStop}
            isRunning={isRunning}
            disabled={composerDisabled}
            disabledReason={composerDisabledReason}
            awaitingAnswer={askPolicy !== null && !askPolicy.locked}
            placeholder={askPolicy?.placeholder}
            tokenUsage={tokenUsage}
            onComposerError={onComposerError}
            paneCards={paneCards}
            browserProfileId={browserProfileId}
            onSelectBrowserProfile={onSelectBrowserProfile}
            browserProfilesEnabled={browserProfilesEnabled}
            browserProfileLocked={browserProfileLocked}
            viewMode={viewMode}
            onViewModeChange={onViewModeChange}
            deepResearchEnabled={deepResearchEnabled}
            researchEnabled={researchEnabled}
            codeAgentsEnabled={codeAgentsEnabled}
            whiteboardEnabled={whiteboardEnabled}
            loopsEnabled={loopsEnabled}
            missionsEnabled={missionsEnabled}
            goalsEnabled={goalsEnabled}
            compressEnabled={compressEnabled}
            commandsEnabled={commandsEnabled}
            skillsEnabled={skillsEnabled}
            attachmentsEnabled={attachmentsEnabled}
          />
          {composerFooter}
          {onOpenIntegrations && (
            <IntegrationsBandSlot
              agentId={agentId}
              onOpenIntegrations={onOpenIntegrations}
            />
          )}
        </>
      )}
    </div>
  );

  // Empty, ready-to-start session: center the title + composer vertically
  // rather than pinning the composer to the bottom of a blank thread. Only
  // when there's nothing to show (no history loading, not a disabled/read-only
  // session) — otherwise fall through to the normal scroll-thread layout.
  if (messages.length === 0 && !isLoadingHistory && !disabled) {
    return (
      // A centred hero is a deliberate desktop look. On a phone it leaves the
      // input floating mid-screen with dead space under it, and the keyboard
      // then opens across that gap. There the composer keeps its usual place
      // at the bottom edge and the greeting takes the space above it.
      // data-transcript: the host's transcript settings shape this, the composer with it.
      <div data-transcript="" className={cn(TRANSCRIPT_WIDTH, "flex flex-1 flex-col items-center justify-end overflow-hidden bg-background px-3 text-base md:justify-center")}>
        <div className="flex w-full flex-1 flex-col justify-center md:flex-none">
          <h2 className="mb-6 text-center text-2xl font-bold text-foreground md:mb-8">
            What should we do ?
          </h2>
        </div>
        <div className="w-full">{composerArea}</div>
      </div>
    );
  }

  return (
    // data-transcript: the host's transcript settings shape this, the composer with it.
    <div data-transcript="" className={cn(TRANSCRIPT_WIDTH, "flex flex-1 flex-col overflow-hidden bg-background text-base")}>
      {/* Loaded history is a new view: it opens at the bottom of the finished conversation, not scrolled down to it.
          Until it settles, height changes follow without animating, so it does not look like it scrolls; live streaming then glides. */}
      <Conversation
        key={isLoadingHistory ? "loading" : "loaded"}
        resize={historySettled ? "smooth" : "instant"}
        className="relative flex-1 min-h-0"
      >
        <ConversationContent className="mx-auto w-full max-w-4xl">
          {isLoadingHistory ? (
            <ConversationEmptyState
              icon={<Loader2Icon className="size-8 animate-spin opacity-40" />}
              title="Loading conversation"
              description="Fetching the session history."
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
                  // Simple mode hides skill_invoked markers entirely.
                  const shown = group.messages.filter(
                    (msg) => !(viewMode === "simple" && msg.systemKind === "skill_invoked"),
                  );
                  if (shown.length === 0) return null;
                  // One run: its markers sit at the in-group pitch, not
                  // the thread's gap between groups.
                  return (
                    <div key={group.messages[0].id} className="space-y-2">
                      {shown.map((msg) => (
                        <OrphanSystemMarker
                          key={msg.id}
                          message={msg}
                          sessionId={sessionId}
                          onRetry={msg.id === activeFailureId ? onRetry : undefined}
                          inRun
                        />
                      ))}
                    </div>
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
                    activeAssistantId={activeAssistantId}
                    lastGlobalIndex={group.lastGlobalIndex}
                    totalMessages={messages.length}
                    isRunning={isRunning}
                    sessionId={sessionId}
                    artifactFallbacks={artifactFallbacks}
                    onFileSelect={onFileSelect}
                    onRetry={groupRetry}
                    viewMode={viewMode}
                    hideTurnSummary={hideTurnSummary}
                    fullReplies={hideComposer}
                  />
                );
              })}
              {showWorkingOnIt && <WorkingOnItIndicator activity={orbActivity} />}
            </>
          )}
        </ConversationContent>
        <ConversationScrollButton />
      </Conversation>

      {composerArea}
    </div>
  );
}

/**
 * Pulls the adapter from context so ChatThread can render the integrations
 * band without receiving the adapter as a prop. Renders nothing unless the
 * adapter supports the Composio methods (handled inside IntegrationsBand).
 */
function IntegrationsBandSlot({
  agentId,
  onOpenIntegrations,
}: {
  agentId?: string;
  onOpenIntegrations: () => void;
}) {
  const { adapter } = useAgentChatAdapterContext();
  return (
    <IntegrationsBand
      agentId={agentId}
      adapter={adapter}
      onOpenIntegrations={onOpenIntegrations}
    />
  );
}

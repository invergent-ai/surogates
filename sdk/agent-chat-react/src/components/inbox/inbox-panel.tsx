// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { formatDistanceToNow } from "date-fns";
import {
  CheckIcon,
  CircleDotIcon,
  ClipboardCheckIcon,
  ExternalLinkIcon,
  InboxIcon,
  MessageSquareIcon,
  ShieldAlertIcon,
  TimerIcon,
  Trash2Icon,
} from "lucide-react";
import { MessageResponse } from "../ai-elements/message";
import {
  type AnswerDraft,
  type InboxQuestion,
  answerText,
  buildInboxResponse,
  parseInboxQuestions,
} from "./inbox-answers";
import { useAnswerWindow } from "./inbox-expiry";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { cn } from "../../lib/utils";
import type {
  AgentChatAdapter,
  AgentChatAskUserQuestionAnswer,
  AgentChatInboxEventStream,
  AgentChatInboxItem,
  AgentChatInboxKind,
  AgentChatInboxList,
  AgentChatInboxStatus,
} from "../../types";

type OnSessionSelect = (sessionId: string, item?: AgentChatInboxItem) => void;

export interface InboxPanelProps {
  adapter: AgentChatAdapter;
  title?: string;
  selectedId?: number | null;
  onSelectedIdChange?: (itemId: number | null) => void;
  onSessionSelect?: OnSessionSelect;
  hideHeader?: boolean;
  limit?: number;
}

const DEFAULT_LIMIT = 50;

type InboxAdapter = AgentChatAdapter & {
  listInbox(input?: Parameters<NonNullable<AgentChatAdapter["listInbox"]>>[0]): Promise<AgentChatInboxList>;
  getInboxItem(input: { itemId: number }): Promise<AgentChatInboxItem>;
  markInboxItemRead(input: { itemId: number }): Promise<AgentChatInboxItem>;
  acknowledgeInboxItem(input: { itemId: number }): Promise<AgentChatInboxItem>;
  deleteInboxItem?(input: { itemId: number }): Promise<void>;
  respondGovernanceInboxItem(input: {
    itemId: number;
    decision: "approve" | "reject";
  }): Promise<AgentChatInboxItem>;
  respondActionRequiredInboxItem?(input: {
    itemId: number;
  }): Promise<AgentChatInboxItem>;
  openInboxStream(): AgentChatInboxEventStream;
};

function requireInboxAdapter(adapter: AgentChatAdapter): InboxAdapter {
  if (
    !adapter.listInbox ||
    !adapter.getInboxItem ||
    !adapter.markInboxItemRead ||
    !adapter.acknowledgeInboxItem ||
    !adapter.respondGovernanceInboxItem ||
    !adapter.openInboxStream
  ) {
    throw new Error("Inbox is not supported by this adapter.");
  }
  return adapter as InboxAdapter;
}

const KIND_LABEL: Record<string, string> = {
  input_required: "Input needed",
  action_required: "Action needed",
  task_complete: "Task complete",
  governance_gate: "Approval",
  progress_checkin: "Progress",
};

function kindLabel(kind: AgentChatInboxKind): string {
  return KIND_LABEL[kind] ?? kind.replace(/[_-]+/g, " ");
}

function statusLabel(status: string): string {
  return status
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatRelative(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return formatDistanceToNow(date, { addSuffix: true });
}

function kindIcon(kind: AgentChatInboxKind) {
  if (kind === "input_required") return MessageSquareIcon;
  if (kind === "action_required") return ExternalLinkIcon;
  if (kind === "task_complete") return ClipboardCheckIcon;
  if (kind === "governance_gate") return ShieldAlertIcon;
  if (kind === "progress_checkin") return TimerIcon;
  return CircleDotIcon;
}

type InboxView = "active" | "history";

// History is asked for by name: a request that omits the status filter
// gets everything except expired, which is exactly the half of history
// worth keeping — a question the agent stopped waiting for, or an item
// that was dismissed.
const STATUSES_BY_VIEW: Record<InboxView, AgentChatInboxStatus[]> = {
  active: ["pending"],
  history: ["acknowledged", "responded", "expired"],
};

function sortItems(items: AgentChatInboxItem[]): AgentChatInboxItem[] {
  // Paging can overlap with a stream update; the first copy of a row
  // wins (callers put the fresher one first) and it is listed once.
  const byId = new Map<number, AgentChatInboxItem>();
  for (const item of items) {
    if (!byId.has(item.id)) byId.set(item.id, item);
  }
  return [...byId.values()].sort((a, b) => {
    if (a.createdAt !== b.createdAt) return a.createdAt > b.createdAt ? -1 : 1;
    return b.id - a.id;
  });
}

function InboxBody({ body, muted }: { body: string; muted?: boolean }) {
  return (
    <MessageResponse
      className={cn("text-sm", muted && "text-muted-foreground")}
    >
      {body}
    </MessageResponse>
  );
}

const FIELD_CLASS =
  "h-9 w-full border border-line bg-background px-2 text-sm text-foreground outline-none focus:border-primary disabled:opacity-50";

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

/**
 * Runs a detail-pane action, reporting failure where the user is looking.
 *
 * Every action here reaches the network, and an unhandled rejection put
 * the button back the way it was with nothing said — indistinguishable
 * from a submit that worked.
 */
function useAction(): {
  error: string | null;
  busy: boolean;
  run: (fallback: string, action: () => Promise<void>) => Promise<void>;
} {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const run = useCallback(
    async (fallback: string, action: () => Promise<void>) => {
      setBusy(true);
      setError(null);
      try {
        await action();
      } catch (err) {
        setError(errorMessage(err, fallback));
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  return { error, busy, run };
}

function ActionError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="text-sm text-destructive">
      {message}
    </p>
  );
}

function ExpiryNote({ label, expired }: { label: string; expired: boolean }) {
  return (
    <p
      className={cn(
        "text-xs",
        expired ? "text-muted-foreground" : "text-foreground",
      )}
    >
      {expired
        ? "The agent stopped waiting for this answer and moved on, so it can no longer be submitted."
        : `${label}. Deleting it will not answer the agent; it only clears it from your inbox.`}
    </p>
  );
}

// <option> values are choice INDEXES, never labels: an agent-supplied
// label could otherwise collide with whatever sentinel marks the
// free-form row. "other" is not a number, so it cannot be an index.
const OTHER_OPTION = "other";

function QuestionInput({
  question,
  draft,
  disabled,
  onPickChoice,
  onChooseOther,
  onType,
}: {
  question: InboxQuestion;
  draft: AnswerDraft | undefined;
  disabled: boolean;
  onPickChoice: (label: string) => void;
  onChooseOther: () => void;
  onType: (value: string) => void;
}) {
  const { prompt, choices, allowOther } = question;
  const typed = draft?.typed ?? "";
  const useOther = !!draft?.useOther;

  if (choices && choices.length > 0) {
    const pickedIndex = choices.findIndex(
      (choice) => choice.label === draft?.picked,
    );
    return (
      <div className="space-y-1.5">
        <select
          aria-label={prompt}
          className={FIELD_CLASS}
          disabled={disabled}
          value={
            useOther
              ? OTHER_OPTION
              : pickedIndex >= 0
                ? String(pickedIndex)
                : ""
          }
          onChange={(event) => {
            const value = event.target.value;
            if (value === OTHER_OPTION) {
              onChooseOther();
              return;
            }
            const choice = choices[Number(value)];
            if (choice) onPickChoice(choice.label);
          }}
        >
          {/* Disabled, so it cannot be chosen back: its value is "" and
              Number("") is 0, which would read as the first choice. */}
          <option value="" disabled>
            Select
          </option>
          {choices.map((choice, index) => (
            <option key={choice.label} value={String(index)}>
              {choice.label}
            </option>
          ))}
          {/* Without this the menu is the only answer the user can
              give, even when the agent said any answer is fine. */}
          {allowOther && <option value={OTHER_OPTION}>Other</option>}
        </select>
        {useOther && (
          <input
            aria-label={`${prompt} (other)`}
            className={FIELD_CLASS}
            placeholder="Type your answer…"
            disabled={disabled}
            value={typed}
            onChange={(event) => onType(event.target.value)}
          />
        )}
      </div>
    );
  }

  return (
    <input
      aria-label={prompt}
      className={FIELD_CLASS}
      disabled={disabled}
      value={typed}
      onChange={(event) => onType(event.target.value)}
    />
  );
}

function InboxDetailActions({
  item,
  adapter,
  onDeleted,
  onSessionSelect,
  children,
}: {
  item: AgentChatInboxItem;
  adapter: InboxAdapter;
  onDeleted: (itemId: number) => Promise<void>;
  onSessionSelect?: OnSessionSelect;
  children?: ReactNode;
}) {
  const { error, busy: deleting, run } = useAction();

  async function deleteItem() {
    if (!adapter.deleteInboxItem) return;
    await run("Failed to delete", () => onDeleted(item.id));
  }

  return (
    <div className="space-y-2">
      <ActionError message={error} />
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          onClick={() => onSessionSelect?.(item.sessionId, item)}
          aria-label="Open session"
        >
          <ExternalLinkIcon className="size-3.5" />
          Open session
        </Button>
        {children}
        {adapter.deleteInboxItem && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => void deleteItem()}
            disabled={deleting}
            aria-label="Delete inbox item"
            title="Delete inbox item"
          >
            <Trash2Icon className="size-3.5" />
            {deleting ? "Deleting" : "Delete"}
          </Button>
        )}
      </div>
    </div>
  );
}

function InputRequiredDetail({
  item,
  adapter,
  onUpdated,
  onDeleted,
  onSessionSelect,
}: {
  item: AgentChatInboxItem;
  adapter: InboxAdapter;
  onUpdated: (item: AgentChatInboxItem) => void;
  onDeleted: (itemId: number) => Promise<void>;
  onSessionSelect?: OnSessionSelect;
}) {
  // Re-parsing on every keystroke would rebuild every question and
  // choice object while the user is only editing an answer.
  const questions = useMemo(() => parseInboxQuestions(item), [item]);
  const [drafts, setDrafts] = useState<Record<string, AnswerDraft>>({});
  const { error, busy: submitting, run } = useAction();
  const { expired, label: expiryLabel } = useAnswerWindow(item.expiresAt);
  const pending = item.status === "pending" && !expired;
  const disabled = !pending || submitting;

  const editDraft = (prompt: string, patch: Partial<AnswerDraft>) =>
    setDrafts((current) => ({
      ...current,
      [prompt]: { ...current[prompt], ...patch },
    }));

  // An empty batch satisfies every(), and the server rejects a
  // submission with no answers in it.
  const canSubmit =
    questions.length > 0 &&
    questions.every((question) => answerText(question, drafts[question.prompt]));

  async function submit() {
    const toolCallId =
      typeof item.payload.tool_call_id === "string"
        ? item.payload.tool_call_id
        : "";
    if (!toolCallId || !canSubmit) return;
    await run("Failed to submit your answer", async () => {
      const responses: AgentChatAskUserQuestionAnswer[] = questions.map(
        (question) => buildInboxResponse(question, drafts[question.prompt]),
      );
      await adapter.submitAskUserQuestionResponse({
        sessionId: item.sessionId,
        toolCallId,
        responses,
      });
      onUpdated(await adapter.getInboxItem({ itemId: item.id }));
    });
  }

  return (
    <div className="space-y-4">
      {item.body && <InboxBody body={item.body} muted />}
      {questions.map((question) => (
        <label key={question.prompt} className="block space-y-1.5">
          <span className="text-sm font-medium text-foreground">
            {question.prompt}
          </span>
          <QuestionInput
            question={question}
            draft={drafts[question.prompt]}
            disabled={disabled}
            onPickChoice={(label) =>
              // Dropping the typed value keeps an abandoned draft from
              // resurfacing if the user switches back to Other.
              editDraft(question.prompt, {
                picked: label,
                typed: "",
                useOther: false,
              })
            }
            onChooseOther={() => editDraft(question.prompt, { useOther: true })}
            onType={(value) => editDraft(question.prompt, { typed: value })}
          />
        </label>
      ))}
      {item.status === "pending" && (
        <ExpiryNote label={expiryLabel} expired={expired} />
      )}
      <ActionError message={error} />
      <InboxDetailActions
        item={item}
        adapter={adapter}
        onDeleted={onDeleted}
        onSessionSelect={onSessionSelect}
      >
        <Button
          type="button"
          size="sm"
          onClick={() => void submit()}
          disabled={disabled || !canSubmit}
          aria-label="Submit inbox response"
        >
          {submitting ? "Submitting" : "Submit"}
        </Button>
      </InboxDetailActions>
    </div>
  );
}

function AckDetail({
  item,
  adapter,
  onUpdated,
  onDeleted,
  onSessionSelect,
}: {
  item: AgentChatInboxItem;
  adapter: InboxAdapter;
  onUpdated: (item: AgentChatInboxItem) => void;
  onDeleted: (itemId: number) => Promise<void>;
  onSessionSelect?: OnSessionSelect;
}) {
  const { error, busy, run } = useAction();
  async function acknowledge() {
    await run("Failed to acknowledge", async () => {
      onUpdated(await adapter.acknowledgeInboxItem({ itemId: item.id }));
    });
  }
  const outcome = typeof item.payload.outcome === "string" ? item.payload.outcome : "";
  const duration =
    typeof item.payload.duration_seconds === "number"
      ? item.payload.duration_seconds
      : null;
  return (
    <div className="space-y-3">
      {outcome && <Badge variant="secondary">{outcome}</Badge>}
      {item.body && <InboxBody body={item.body} />}
      {typeof item.payload.error === "string" && (
        <pre className="overflow-x-auto bg-destructive/10 p-3 text-xs text-destructive">
          {item.payload.error}
        </pre>
      )}
      {duration !== null && (
        <p className="text-xs text-muted-foreground">
          Duration: {Math.round(duration / 60)} min ({duration} s)
        </p>
      )}
      <ActionError message={error} />
      <InboxDetailActions
        item={item}
        adapter={adapter}
        onDeleted={onDeleted}
        onSessionSelect={onSessionSelect}
      >
        {item.status === "pending" && (
          <Button
            type="button"
            size="sm"
            onClick={() => void acknowledge()}
            disabled={busy}
            aria-label="Acknowledge inbox item"
          >
            <CheckIcon className="size-3.5" />
            {busy ? "Saving" : "Acknowledge"}
          </Button>
        )}
      </InboxDetailActions>
    </div>
  );
}

function GovernanceDetail({
  item,
  adapter,
  onUpdated,
  onDeleted,
  onSessionSelect,
}: {
  item: AgentChatInboxItem;
  adapter: InboxAdapter;
  onUpdated: (item: AgentChatInboxItem) => void;
  onDeleted: (itemId: number) => Promise<void>;
  onSessionSelect?: OnSessionSelect;
}) {
  const { error, busy, run } = useAction();
  async function decide(decision: "approve" | "reject") {
    await run(`Failed to ${decision}`, async () => {
      onUpdated(
        await adapter.respondGovernanceInboxItem({ itemId: item.id, decision }),
      );
    });
  }
  const toolName =
    typeof item.payload.tool_name === "string" ? item.payload.tool_name : "tool";
  const args =
    typeof item.payload.arguments_excerpt === "string"
      ? item.payload.arguments_excerpt
      : "";
  const reason =
    typeof item.payload.deny_reason === "string" ? item.payload.deny_reason : "";
  const disabled = item.status !== "pending" || busy;
  return (
    <div className="space-y-4">
      <div>
        <div className="text-base font-semibold text-foreground">
          Approve {toolName}?
        </div>
        {reason && <p className="mt-1 text-sm text-muted-foreground">{reason}</p>}
      </div>
      {args && (
        <pre className="overflow-x-auto border border-line bg-muted p-3 text-xs">
          {args}
        </pre>
      )}
      <ActionError message={error} />
      <InboxDetailActions
        item={item}
        adapter={adapter}
        onDeleted={onDeleted}
        onSessionSelect={onSessionSelect}
      >
        <Button
          type="button"
          size="sm"
          disabled={disabled}
          onClick={() => void decide("approve")}
        >
          Approve
        </Button>
        <Button
          type="button"
          size="sm"
          variant="destructive"
          disabled={disabled}
          onClick={() => void decide("reject")}
        >
          Reject
        </Button>
      </InboxDetailActions>
    </div>
  );
}

function ActionRequiredDetail({
  item,
  adapter,
  onUpdated,
  onDeleted,
  onSessionSelect,
}: {
  item: AgentChatInboxItem;
  adapter: InboxAdapter;
  onUpdated: (item: AgentChatInboxItem) => void;
  onDeleted: (itemId: number) => Promise<void>;
  onSessionSelect?: OnSessionSelect;
}) {
  const { error, busy: submitting, run } = useAction();
  const actionType =
    typeof item.payload.action_type === "string"
      ? item.payload.action_type
      : "";
  const context =
    typeof item.payload.context === "string" ? item.payload.context : "";
  const disabled =
    item.status !== "pending" || submitting || !adapter.respondActionRequiredInboxItem;

  async function complete() {
    const respond = adapter.respondActionRequiredInboxItem;
    if (!respond) return;
    await run("Failed to mark the action complete", async () => {
      onUpdated(await respond({ itemId: item.id }));
    });
  }

  return (
    <div className="space-y-4">
      {item.body && <InboxBody body={item.body} />}
      {context && context !== item.body && (
        <p className="whitespace-pre-wrap text-sm text-muted-foreground">
          {context}
        </p>
      )}
      {actionType && <Badge variant="secondary">{actionType}</Badge>}
      <ActionError message={error} />
      <InboxDetailActions
        item={item}
        adapter={adapter}
        onDeleted={onDeleted}
        onSessionSelect={onSessionSelect}
      >
        <Button
          type="button"
          size="sm"
          onClick={() => void complete()}
          disabled={disabled}
          aria-label="Mark action complete"
        >
          <CheckIcon className="size-3.5" />
          {submitting ? "Marking" : "I completed this"}
        </Button>
      </InboxDetailActions>
    </div>
  );
}

function ProgressDetail({
  item,
  adapter,
  onUpdated,
  onDeleted,
  onSessionSelect,
}: {
  item: AgentChatInboxItem;
  adapter: InboxAdapter;
  onUpdated: (item: AgentChatInboxItem) => void;
  onDeleted: (itemId: number) => Promise<void>;
  onSessionSelect?: OnSessionSelect;
}) {
  const { error, busy, run } = useAction();
  async function acknowledge() {
    await run("Failed to acknowledge", async () => {
      onUpdated(await adapter.acknowledgeInboxItem({ itemId: item.id }));
    });
  }
  const rows: Array<[string, unknown]> = [];
  for (const row of [
    ["Iterations", item.payload.iterations] as [string, unknown],
    ["Last tool", item.payload.last_tool] as [string, unknown],
    ["Elapsed", item.payload.elapsed_seconds] as [string, unknown],
  ]) {
    if (row[1] !== undefined && row[1] !== null && row[1] !== "") {
      rows.push(row);
    }
  }
  return (
    <div className="space-y-3">
      {item.body && <InboxBody body={item.body} />}
      {rows.length > 0 && (
        <dl className="space-y-1 text-xs text-muted-foreground">
          {rows.map(([label, value]) => (
            <div key={String(label)} className="flex gap-2">
              <dt className="w-20 shrink-0">{String(label)}</dt>
              <dd className="min-w-0">{String(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      <ActionError message={error} />
      <InboxDetailActions
        item={item}
        adapter={adapter}
        onDeleted={onDeleted}
        onSessionSelect={onSessionSelect}
      >
        {item.status === "pending" && (
          <Button
            type="button"
            size="sm"
            onClick={() => void acknowledge()}
            disabled={busy}
            aria-label="Acknowledge inbox item"
          >
            <CheckIcon className="size-3.5" />
            {busy ? "Saving" : "Acknowledge"}
          </Button>
        )}
      </InboxDetailActions>
    </div>
  );
}

function InboxDetail({
  item,
  adapter,
  onUpdated,
  onDeleted,
  onSessionSelect,
}: {
  item: AgentChatInboxItem;
  adapter: InboxAdapter;
  onUpdated: (item: AgentChatInboxItem) => void;
  onDeleted: (itemId: number) => Promise<void>;
  onSessionSelect?: OnSessionSelect;
}) {
  const Icon = kindIcon(item.kind);

  return (
    <section className="min-w-0 flex-1 overflow-y-auto p-6">
      <div className="mb-5 flex min-w-0 items-start gap-3">
        <div className="mt-0.5 flex size-8 shrink-0 items-center justify-center border border-line text-muted-foreground">
          <Icon className="size-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary">{kindLabel(item.kind)}</Badge>
            <Badge variant={item.status === "pending" ? "default" : "secondary"}>
              {statusLabel(item.status)}
            </Badge>
          </div>
          <h2 className="mt-2 text-lg font-semibold leading-snug text-foreground">
            {item.title}
          </h2>
        </div>
      </div>

      {item.kind === "input_required" ? (
        <InputRequiredDetail
          item={item}
          adapter={adapter}
          onUpdated={onUpdated}
          onDeleted={onDeleted}
          onSessionSelect={onSessionSelect}
        />
      ) : item.kind === "action_required" ? (
        <ActionRequiredDetail
          item={item}
          adapter={adapter}
          onUpdated={onUpdated}
          onDeleted={onDeleted}
          onSessionSelect={onSessionSelect}
        />
      ) : item.kind === "governance_gate" ? (
        <GovernanceDetail
          item={item}
          adapter={adapter}
          onUpdated={onUpdated}
          onDeleted={onDeleted}
          onSessionSelect={onSessionSelect}
        />
      ) : item.kind === "progress_checkin" ? (
        <ProgressDetail
          item={item}
          adapter={adapter}
          onUpdated={onUpdated}
          onDeleted={onDeleted}
          onSessionSelect={onSessionSelect}
        />
      ) : (
        <AckDetail
          item={item}
          adapter={adapter}
          onUpdated={onUpdated}
          onDeleted={onDeleted}
          onSessionSelect={onSessionSelect}
        />
      )}
    </section>
  );
}

export function InboxPanel({
  adapter,
  title = "Inbox",
  selectedId,
  onSelectedIdChange,
  onSessionSelect,
  hideHeader = false,
  limit = DEFAULT_LIMIT,
}: InboxPanelProps) {
  const inboxAdapter = useMemo(() => requireInboxAdapter(adapter), [adapter]);
  const [items, setItems] = useState<AgentChatInboxItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [internalSelectedId, setInternalSelectedId] = useState<number | null>(null);
  const [view, setView] = useState<InboxView>("active");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);
  const selectedItemId = selectedId ?? internalSelectedId;
  const selectedItem = useMemo(
    () => items.find((item) => item.id === selectedItemId) ?? null,
    [items, selectedItemId],
  );

  // Read through a ref, never closed over: an action started before a
  // tab switch resolves after it, and a callback holding the old view
  // would file the result under the list the user has since left.
  const viewRef = useRef(view);
  viewRef.current = view;

  const applyItem = useCallback((nextItem: AgentChatInboxItem) => {
    setItems((current) => {
      // A row already on screen updates in place, whatever its new
      // status: the user just acted on it and the result — Acknowledged,
      // Responded — is the confirmation. It leaves the view on the next
      // load, the way a read mail stays put until you come back to the
      // folder.
      //
      // A row that is not on screen is an insert, which is how the
      // stream announces something new; mapping over the existing rows
      // dropped those entirely and froze the list at whatever it held
      // when the page opened. Only insert what belongs here, so a nudge
      // about a fresh pending item cannot land in History.
      if (current.some((item) => item.id === nextItem.id)) {
        return sortItems([nextItem, ...current]);
      }
      return STATUSES_BY_VIEW[viewRef.current].includes(nextItem.status)
        ? sortItems([nextItem, ...current])
        : current;
    });
  }, []);

  const selectItem = useCallback(
    async (itemId: number) => {
      if (selectedId === undefined) setInternalSelectedId(itemId);
      onSelectedIdChange?.(itemId);
      const item = await inboxAdapter.getInboxItem({ itemId });
      applyItem(item);
      if (!item.readAt) {
        applyItem(await inboxAdapter.markInboxItemRead({ itemId }));
      }
    },
    [applyItem, inboxAdapter, onSelectedIdChange, selectedId],
  );

  const load = useCallback(
    async (cursor?: string | null) => {
      const id = ++requestId.current;
      setLoading(true);
      try {
        const response = await inboxAdapter.listInbox({
          status: STATUSES_BY_VIEW[view],
          cursor: cursor ?? undefined,
          limit,
        });
        if (id !== requestId.current) return;
        // Merged, never replaced. A first page is only ever loaded into
        // an empty list — mount, a view switch, a retry — so the only
        // thing merging preserves is an item a nudge inserted while this
        // request was in flight, which a replace would silently drop.
        setItems((current) => sortItems([...response.items, ...current]));
        setNextCursor(response.nextCursor);
        setError(null);
      } catch (err) {
        if (id === requestId.current) {
          setError(errorMessage(err, "Failed to load inbox"));
        }
      } finally {
        if (id === requestId.current) setLoading(false);
      }
    },
    [inboxAdapter, limit, view],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  useEffect(() => {
    const stream = inboxAdapter.openInboxStream();
    stream.addEventListener("item", (event) => {
      // A stream frame is not a trusted shape: a malformed one must not
      // throw out of the listener and take the subscription with it.
      let itemId: unknown;
      try {
        itemId = (JSON.parse(event.data) as { item_id?: unknown }).item_id;
      } catch {
        return;
      }
      if (typeof itemId !== "number") return;
      // Only the Active view moves on its own — a nudge means something
      // new is pending, which by definition is not history. Read from
      // the ref so a tab click does not tear down the connection.
      if (viewRef.current !== "active") return;
      void inboxAdapter.getInboxItem({ itemId }).then(applyItem, () => undefined);
    });
    return () => stream.close();
  }, [applyItem, inboxAdapter]);

  function updateSelectedItem(item: AgentChatInboxItem) {
    applyItem(item);
  }

  const selectView = useCallback(
    (next: InboxView) => {
      if (next === view) return;
      // The two views share no items, so keeping the old ones on screen
      // while the new page loads would show the wrong list.
      setView(next);
      setItems([]);
      setNextCursor(null);
      setError(null);
      if (selectedId === undefined) setInternalSelectedId(null);
      onSelectedIdChange?.(null);
    },
    [onSelectedIdChange, selectedId, view],
  );

  const deleteItem = useCallback(
    async (itemId: number) => {
      if (!inboxAdapter.deleteInboxItem) return;
      await inboxAdapter.deleteInboxItem({ itemId });
      setItems((current) => current.filter((item) => item.id !== itemId));
      if (selectedItemId === itemId) {
        if (selectedId === undefined) setInternalSelectedId(null);
        onSelectedIdChange?.(null);
      }
    },
    [inboxAdapter, onSelectedIdChange, selectedId, selectedItemId],
  );

  return (
    <div className="flex h-full min-h-0 min-w-0 bg-background text-foreground">
      <aside className="flex w-80 min-w-72 max-w-sm shrink-0 flex-col border-r border-line">
        {!hideHeader && (
          <div className="flex min-h-14 items-center gap-2 border-b border-line px-4">
            <InboxIcon className="size-4 text-muted-foreground" />
            <h1 className="font-semibold">{title}</h1>
          </div>
        )}
        <div className="flex gap-1 border-b border-line px-2 py-2">
          {(["active", "history"] as const).map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => selectView(value)}
              className={cn(
                "px-2 py-1 text-xs capitalize transition-colors",
                view === value
                  ? "bg-line font-medium text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {value}
            </button>
          ))}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {error && items.length === 0 && (
            <div className="space-y-2 px-4 py-3">
              <p className="text-sm text-destructive">{error}</p>
              <Button type="button" size="sm" onClick={() => void load(null)}>
                Try again
              </Button>
            </div>
          )}
          {items.length === 0 && !error && !loading && (
            <div className="px-4 py-8 text-sm text-muted-foreground">
              {view === "active"
                ? "Nothing needs you right now"
                : "No history yet"}
            </div>
          )}
          {items.map((item) => {
            const Icon = kindIcon(item.kind);
            const selected = item.id === selectedItemId;
            return (
              <button
                key={item.id}
                type="button"
                aria-label={`Open inbox item ${item.title}`}
                className={cn(
                  "flex w-full min-w-0 gap-3 border-l-2 px-3 py-3 text-left transition-colors hover:bg-input",
                  selected
                    ? "border-l-primary bg-line text-foreground"
                    : "border-l-transparent text-subtle",
                  !item.readAt && "font-medium text-foreground",
                )}
                onClick={() => void selectItem(item.id)}
              >
                <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1">
                  <span className="flex min-w-0 items-center justify-between gap-2">
                    <span className="truncate text-xs uppercase tracking-wide text-muted-foreground">
                      {kindLabel(item.kind)}
                    </span>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {formatRelative(item.createdAt)}
                    </span>
                  </span>
                  <span className="mt-1 block truncate text-sm">{item.title}</span>
                  <span className="mt-1 block text-xs text-muted-foreground">
                    {statusLabel(item.status)}
                  </span>
                </span>
              </button>
            );
          })}
          {nextCursor && (
            <button
              type="button"
              className="w-full px-3 py-3 text-sm text-muted-foreground hover:bg-input"
              onClick={() => void load(nextCursor)}
              disabled={loading}
            >
              {loading ? "Loading" : "Load more"}
            </button>
          )}
        </div>
      </aside>
      {selectedItem ? (
        <InboxDetail
          // Remount per item: two items of the same kind render the same
          // component, so without this the previous one's failure message
          // and half-typed answers carry over to the next.
          key={selectedItem.id}
          item={selectedItem}
          adapter={inboxAdapter}
          onUpdated={updateSelectedItem}
          onDeleted={deleteItem}
          onSessionSelect={onSessionSelect}
        />
      ) : (
        <main className="flex min-w-0 flex-1 items-center justify-center px-4 text-sm text-muted-foreground">
          Select an item
        </main>
      )}
    </div>
  );
}

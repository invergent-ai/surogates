// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { cn } from "../../lib/utils";
import type {
  AgentChatAdapter,
  AgentChatClarifyAnswer,
  AgentChatInboxEventStream,
  AgentChatInboxItem,
  AgentChatInboxKind,
  AgentChatInboxList,
} from "../../types";

export interface InboxPanelProps {
  adapter: AgentChatAdapter;
  title?: string;
  selectedId?: number | null;
  onSelectedIdChange?: (itemId: number | null) => void;
  onSessionSelect?: (sessionId: string) => void;
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

function sortItems(items: AgentChatInboxItem[]): AgentChatInboxItem[] {
  return [...items].sort((a, b) => {
    if (a.createdAt !== b.createdAt) return a.createdAt > b.createdAt ? -1 : 1;
    return b.id - a.id;
  });
}

function QuestionInput({
  prompt,
  choices,
  disabled,
  value,
  onChange,
}: {
  prompt: string;
  choices?: Array<{ label: string; description?: string }>;
  disabled: boolean;
  value: string;
  onChange: (value: string) => void;
}) {
  if (choices && choices.length > 0) {
    return (
      <select
        aria-label={prompt}
        className="h-9 w-full border border-line bg-background px-2 text-sm text-foreground outline-none focus:border-primary disabled:opacity-50"
        disabled={disabled}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Select</option>
        {choices.map((choice) => (
          <option key={choice.label} value={choice.label}>
            {choice.label}
          </option>
        ))}
      </select>
    );
  }

  return (
    <input
      aria-label={prompt}
      className="h-9 w-full border border-line bg-background px-2 text-sm text-foreground outline-none focus:border-primary disabled:opacity-50"
      disabled={disabled}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

function InputRequiredDetail({
  item,
  adapter,
  onUpdated,
}: {
  item: AgentChatInboxItem;
  adapter: InboxAdapter;
  onUpdated: (item: AgentChatInboxItem) => void;
}) {
  const questions = Array.isArray(item.payload.questions)
    ? (item.payload.questions as Array<{
        prompt?: unknown;
        choices?: unknown;
      }>)
        .map((question) => ({
          prompt: typeof question.prompt === "string" ? question.prompt : "",
          choices: Array.isArray(question.choices)
            ? (question.choices as Array<{ label?: unknown; description?: unknown }>)
                .map((choice) => ({
                  label: typeof choice.label === "string" ? choice.label : "",
                  description:
                    typeof choice.description === "string"
                      ? choice.description
                      : undefined,
                }))
                .filter((choice) => choice.label)
            : undefined,
        }))
        .filter((question) => question.prompt)
    : [];
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const disabled = item.status !== "pending" || submitting;
  const canSubmit = questions.every((question) => answers[question.prompt]?.trim());

  async function submit() {
    const toolCallId =
      typeof item.payload.tool_call_id === "string"
        ? item.payload.tool_call_id
        : "";
    if (!toolCallId || !canSubmit) return;
    setSubmitting(true);
    try {
      const responses: AgentChatClarifyAnswer[] = questions.map((question) => ({
        question: question.prompt,
        answer: answers[question.prompt]?.trim() ?? "",
        is_other: false,
      }));
      await adapter.submitClarifyResponse({
        sessionId: item.sessionId,
        toolCallId,
        responses,
      });
      onUpdated(await adapter.getInboxItem({ itemId: item.id }));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-4">
      {item.body && (
        <p className="whitespace-pre-wrap text-sm text-muted-foreground">
          {item.body}
        </p>
      )}
      {questions.map((question) => (
        <label key={question.prompt} className="block space-y-1.5">
          <span className="text-sm font-medium text-foreground">
            {question.prompt}
          </span>
          <QuestionInput
            prompt={question.prompt}
            choices={question.choices}
            disabled={disabled}
            value={answers[question.prompt] ?? ""}
            onChange={(value) =>
              setAnswers((current) => ({
                ...current,
                [question.prompt]: value,
              }))
            }
          />
        </label>
      ))}
      <Button
        type="button"
        size="sm"
        onClick={() => void submit()}
        disabled={disabled || !canSubmit}
        aria-label="Submit inbox response"
      >
        {submitting ? "Submitting" : "Submit"}
      </Button>
    </div>
  );
}

function AckDetail({
  item,
  adapter,
  onUpdated,
}: {
  item: AgentChatInboxItem;
  adapter: InboxAdapter;
  onUpdated: (item: AgentChatInboxItem) => void;
}) {
  async function acknowledge() {
    onUpdated(await adapter.acknowledgeInboxItem({ itemId: item.id }));
  }
  const outcome = typeof item.payload.outcome === "string" ? item.payload.outcome : "";
  const duration =
    typeof item.payload.duration_seconds === "number"
      ? item.payload.duration_seconds
      : null;
  return (
    <div className="space-y-3">
      {outcome && <Badge variant="secondary">{outcome}</Badge>}
      {item.body && <p className="whitespace-pre-wrap text-sm">{item.body}</p>}
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
      {item.status === "pending" && (
        <Button
          type="button"
          size="sm"
          onClick={() => void acknowledge()}
          aria-label="Acknowledge inbox item"
        >
          <CheckIcon className="size-3.5" />
          Acknowledge
        </Button>
      )}
    </div>
  );
}

function GovernanceDetail({
  item,
  adapter,
  onUpdated,
}: {
  item: AgentChatInboxItem;
  adapter: InboxAdapter;
  onUpdated: (item: AgentChatInboxItem) => void;
}) {
  async function decide(decision: "approve" | "reject") {
    onUpdated(
      await adapter.respondGovernanceInboxItem({ itemId: item.id, decision }),
    );
  }
  const toolName =
    typeof item.payload.tool_name === "string" ? item.payload.tool_name : "tool";
  const args =
    typeof item.payload.arguments_excerpt === "string"
      ? item.payload.arguments_excerpt
      : "";
  const reason =
    typeof item.payload.deny_reason === "string" ? item.payload.deny_reason : "";
  const disabled = item.status !== "pending";
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
      <div className="flex gap-2">
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
      </div>
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
  onSessionSelect?: (sessionId: string) => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const actionType =
    typeof item.payload.action_type === "string"
      ? item.payload.action_type
      : "";
  const context =
    typeof item.payload.context === "string" ? item.payload.context : "";
  const disabled =
    item.status !== "pending" || submitting || !adapter.respondActionRequiredInboxItem;
  const deleteDisabled = deleting || !adapter.deleteInboxItem;

  async function complete() {
    if (!adapter.respondActionRequiredInboxItem) return;
    setSubmitting(true);
    try {
      onUpdated(await adapter.respondActionRequiredInboxItem({ itemId: item.id }));
    } finally {
      setSubmitting(false);
    }
  }

  async function deleteItem() {
    if (!adapter.deleteInboxItem) return;
    setDeleting(true);
    try {
      await onDeleted(item.id);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-4">
      {item.body && <p className="whitespace-pre-wrap text-sm">{item.body}</p>}
      {context && context !== item.body && (
        <p className="whitespace-pre-wrap text-sm text-muted-foreground">
          {context}
        </p>
      )}
      {actionType && <Badge variant="secondary">{actionType}</Badge>}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          onClick={() => onSessionSelect?.(item.sessionId)}
          aria-label="Open session for action"
        >
          <ExternalLinkIcon className="size-3.5" />
          Open session
        </Button>
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
        {adapter.deleteInboxItem && (
          <Button
            type="button"
            size="sm"
            variant="destructive"
            onClick={() => void deleteItem()}
            disabled={deleteDisabled}
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

function ProgressDetail({
  item,
  adapter,
  onUpdated,
}: {
  item: AgentChatInboxItem;
  adapter: InboxAdapter;
  onUpdated: (item: AgentChatInboxItem) => void;
}) {
  async function acknowledge() {
    onUpdated(await adapter.acknowledgeInboxItem({ itemId: item.id }));
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
      {item.body && <p className="whitespace-pre-wrap text-sm">{item.body}</p>}
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
      {item.status === "pending" && (
        <Button
          type="button"
          size="sm"
          onClick={() => void acknowledge()}
          aria-label="Acknowledge inbox item"
        >
          <CheckIcon className="size-3.5" />
          Acknowledge
        </Button>
      )}
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
  onSessionSelect?: (sessionId: string) => void;
}) {
  const Icon = kindIcon(item.kind);
  const [deleting, setDeleting] = useState(false);

  async function deleteItem() {
    if (!adapter.deleteInboxItem) return;
    setDeleting(true);
    try {
      await onDeleted(item.id);
    } finally {
      setDeleting(false);
    }
  }

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
          <button
            type="button"
            className="mt-1 text-xs text-muted-foreground hover:text-foreground"
            onClick={() => onSessionSelect?.(item.sessionId)}
          >
            Open session
          </button>
        </div>
        {adapter.deleteInboxItem && item.kind !== "action_required" && (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            disabled={deleting}
            onClick={() => void deleteItem()}
            aria-label="Delete inbox item"
            title="Delete inbox item"
          >
            <Trash2Icon className="size-4" />
          </Button>
        )}
      </div>

      {item.kind === "input_required" ? (
        <InputRequiredDetail item={item} adapter={adapter} onUpdated={onUpdated} />
      ) : item.kind === "action_required" ? (
        <ActionRequiredDetail
          item={item}
          adapter={adapter}
          onUpdated={onUpdated}
          onDeleted={onDeleted}
          onSessionSelect={onSessionSelect}
        />
      ) : item.kind === "governance_gate" ? (
        <GovernanceDetail item={item} adapter={adapter} onUpdated={onUpdated} />
      ) : item.kind === "progress_checkin" ? (
        <ProgressDetail item={item} adapter={adapter} onUpdated={onUpdated} />
      ) : (
        <AckDetail item={item} adapter={adapter} onUpdated={onUpdated} />
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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);
  const selectedItemId = selectedId ?? internalSelectedId;
  const selectedItem = useMemo(
    () => items.find((item) => item.id === selectedItemId) ?? null,
    [items, selectedItemId],
  );

  const applyItem = useCallback((nextItem: AgentChatInboxItem) => {
    setItems((current) =>
      sortItems(
        current.map((item) => (item.id === nextItem.id ? nextItem : item)),
      ),
    );
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
      setError(null);
      try {
        const response = await inboxAdapter.listInbox({
          cursor: cursor ?? undefined,
          limit,
        });
        if (id !== requestId.current) return;
        setItems((current) =>
          sortItems(cursor ? [...current, ...response.items] : response.items),
        );
        setNextCursor(response.nextCursor);
      } catch (err) {
        if (id === requestId.current) {
          setError(err instanceof Error ? err.message : "Failed to load inbox");
        }
      } finally {
        if (id === requestId.current) setLoading(false);
      }
    },
    [inboxAdapter, limit],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  useEffect(() => {
    const stream = inboxAdapter.openInboxStream();
    stream.addEventListener("item", (event) => {
      const payload = JSON.parse(event.data) as { item_id?: unknown };
      if (typeof payload.item_id !== "number") return;
      void inboxAdapter.getInboxItem({ itemId: payload.item_id }).then(applyItem);
    });
    return () => stream.close();
  }, [applyItem, inboxAdapter]);

  function updateSelectedItem(item: AgentChatInboxItem) {
    applyItem(item);
  }

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
        <div className="min-h-0 flex-1 overflow-y-auto">
          {error && (
            <div className="px-4 py-3 text-sm text-destructive">{error}</div>
          )}
          {!error && items.length === 0 && !loading && (
            <div className="px-4 py-8 text-sm text-muted-foreground">
              No inbox items
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

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { parseError } from "./_errors";
import { authFetch } from "./auth";
import type {
  AgentChatInboxItem,
  AgentChatInboxKind,
  AgentChatInboxList,
  AgentChatInboxListInput,
  AgentChatInboxStatus,
} from "@invergent/agent-chat-react";

interface InboxItemResponse {
  id: number;
  org_id: string;
  user_id: string;
  session_id: string;
  source_event_id: number;
  kind: AgentChatInboxKind;
  status: AgentChatInboxStatus;
  title: string;
  body: string | null;
  payload: Record<string, unknown>;
  action_ref: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  read_at: string | null;
  responded_at: string | null;
  agent_id?: string | null;
  agent_slug?: string | null;
}

interface InboxListResponse {
  items: InboxItemResponse[];
  next_cursor: string | null;
}

function toInboxItem(item: InboxItemResponse): AgentChatInboxItem {
  return {
    id: item.id,
    orgId: item.org_id,
    userId: item.user_id,
    sessionId: item.session_id,
    sourceEventId: item.source_event_id,
    kind: item.kind,
    status: item.status,
    title: item.title,
    body: item.body,
    payload: item.payload,
    actionRef: item.action_ref,
    createdAt: item.created_at,
    updatedAt: item.updated_at,
    readAt: item.read_at,
    respondedAt: item.responded_at,
    agentId: item.agent_id ?? null,
    agentSlug: item.agent_slug ?? null,
  };
}

function listUrl(input: AgentChatInboxListInput = {}): string {
  const params = new URLSearchParams();
  if (input.status) params.set("status", input.status);
  if (input.kind) params.set("kind", input.kind);
  if (input.sessionId) params.set("session_id", input.sessionId);
  if (input.cursor) params.set("cursor", input.cursor);
  if (input.limit) params.set("limit", String(input.limit));
  const qs = params.toString();
  return `/api/v1/inbox${qs ? `?${qs}` : ""}`;
}

export async function listInbox(
  input: AgentChatInboxListInput = {},
): Promise<AgentChatInboxList> {
  const response = await authFetch(listUrl(input));
  if (!response.ok) return parseError(response, "Failed to fetch inbox");
  const body = (await response.json()) as InboxListResponse;
  return {
    items: body.items.map(toInboxItem),
    nextCursor: body.next_cursor,
  };
}

export async function getInboxItem(itemId: number): Promise<AgentChatInboxItem> {
  const response = await authFetch(`/api/v1/inbox/${itemId}`);
  if (!response.ok) return parseError(response, "Failed to fetch inbox item");
  return toInboxItem((await response.json()) as InboxItemResponse);
}

export async function markInboxItemRead(
  itemId: number,
): Promise<AgentChatInboxItem> {
  const response = await authFetch(`/api/v1/inbox/${itemId}/read`, {
    method: "POST",
  });
  if (!response.ok) {
    return parseError(response, "Failed to mark inbox item read");
  }
  return toInboxItem((await response.json()) as InboxItemResponse);
}

export async function acknowledgeInboxItem(
  itemId: number,
): Promise<AgentChatInboxItem> {
  const response = await authFetch(`/api/v1/inbox/${itemId}/ack`, {
    method: "POST",
  });
  if (!response.ok) {
    return parseError(response, "Failed to acknowledge inbox item");
  }
  return toInboxItem((await response.json()) as InboxItemResponse);
}

export async function deleteInboxItem(itemId: number): Promise<void> {
  const response = await authFetch(`/api/v1/inbox/${itemId}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    return parseError(response, "Failed to delete inbox item");
  }
}

export async function respondGovernanceInboxItem(
  itemId: number,
  decision: "approve" | "reject",
): Promise<AgentChatInboxItem> {
  const response = await authFetch(`/api/v1/inbox/${itemId}/respond`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision }),
  });
  if (!response.ok) {
    return parseError(response, "Failed to respond to inbox item");
  }
  return toInboxItem((await response.json()) as InboxItemResponse);
}

export async function respondActionRequiredInboxItem(
  itemId: number,
): Promise<AgentChatInboxItem> {
  const response = await authFetch(`/api/v1/inbox/${itemId}/respond`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ completed: true }),
  });
  if (!response.ok) {
    return parseError(response, "Failed to respond to inbox item");
  }
  return toInboxItem((await response.json()) as InboxItemResponse);
}

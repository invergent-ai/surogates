// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { authFetch } from "./auth";
import type { Session, SessionCreateRequest } from "@/types/session";

export interface SessionListResponse {
  sessions: Session[];
  total: number;
}

export async function listSessions(params?: {
  limit?: number;
  offset?: number;
}): Promise<SessionListResponse> {
  const search = new URLSearchParams();
  if (params?.limit != null) search.append("limit", String(params.limit));
  if (params?.offset != null) search.append("offset", String(params.offset));

  const response = await authFetch(`/api/v1/sessions?${search}`);
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Failed to fetch sessions");
  }
  return (await response.json()) as SessionListResponse;
}

export async function getSession(sessionId: string): Promise<Session> {
  const response = await authFetch(`/api/v1/sessions/${sessionId}`);
  if (!response.ok) throw new Error("Failed to fetch session");
  return (await response.json()) as Session;
}

export async function createSession(
  body: SessionCreateRequest,
): Promise<Session> {
  const response = await authFetch("/api/v1/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error("Failed to create session");
  return (await response.json()) as Session;
}

export async function sendMessage(
  sessionId: string,
  content: string,
): Promise<{ event_id: number; status: string }> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    },
  );
  if (!response.ok) throw new Error("Failed to send message");
  return (await response.json()) as { event_id: number; status: string };
}

export async function confirmDisclosure(sessionId: string): Promise<void> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/confirm-disclosure`,
    { method: "POST" },
  );
  if (!response.ok) throw new Error("Failed to confirm disclosure");
}

export async function deleteSession(sessionId: string): Promise<void> {
  const response = await authFetch(`/api/v1/sessions/${sessionId}`, {
    method: "DELETE",
  });
  if (!response.ok) throw new Error("Failed to delete session");
}

export async function pauseSession(sessionId: string): Promise<void> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/pause`,
    { method: "POST" },
  );
  if (!response.ok) throw new Error("Failed to pause session");
}

export async function resumeSession(sessionId: string): Promise<void> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/resume`,
    { method: "POST" },
  );
  if (!response.ok) throw new Error("Failed to resume session");
}

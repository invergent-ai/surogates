// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { authFetch } from "./auth";
import { errorDetailMessage } from "./_errors";
import type {
  Session,
  SessionChildrenResponse,
  SessionCreateRequest,
  SessionTreeResponse,
  ScheduledWorkListResponse,
} from "@/types/session";

export interface SessionListResponse {
  sessions: Session[];
  total: number;
}

export interface BrowserStateResponse {
  status: "live" | "user-control";
  control_owner: string | null;
  live_view_path: string;
}

export interface BrowserControlResponse {
  outcome: "granted" | "refreshed" | "conflict";
  owner_user_id: string;
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
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Failed to fetch sessions");
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
  images?: { data: string; mime_type: string }[],
  attachments?: {
    path: string;
    filename: string;
    mime_type?: string;
    size?: number;
  }[],
): Promise<{ event_id: number; status: string }> {
  const payload: Record<string, unknown> = { content };
  if (images?.length) {
    payload.images = images;
  }
  if (attachments?.length) {
    payload.attachments = attachments;
  }
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) throw new Error("Failed to send message");
  return (await response.json()) as { event_id: number; status: string };
}

export async function defineOutcome(
  sessionId: string,
  input: {
    description: string;
    rubric: string;
    maxIterations?: number;
  },
): Promise<{
  events: Array<{
    type: "user.define_outcome";
    event_id: number;
    outcome_id: string;
    processed_at: string;
  }>;
}> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/events`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        events: [
          {
            type: "user.define_outcome",
            description: input.description,
            rubric: {
              type: "text",
              content: input.rubric,
            },
            max_iterations: input.maxIterations,
          },
        ],
      }),
    },
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Failed to define outcome");
  }
  return (await response.json()) as {
    events: Array<{
      type: "user.define_outcome";
      event_id: number;
      outcome_id: string;
      processed_at: string;
    }>;
  };
}

/**
 * One-shot pull of the session event log after a cursor.  Non-streaming
 * companion to the SSE endpoint, used by the SDK's reconciliation backstop
 * to catch up whatever the live stream missed.
 */
export async function pollSessionEvents(
  sessionId: string,
  params?: { afterId?: number; limit?: number },
): Promise<{
  events: Array<{ id: number; type: string; data: Record<string, unknown> }>;
  hasMore: boolean;
}> {
  const qs = new URLSearchParams();
  if (params?.afterId != null) qs.set("after", String(params.afterId));
  if (params?.limit != null) qs.set("limit", String(params.limit));
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/events/poll${
      qs.toString() ? `?${qs}` : ""
    }`,
  );
  if (!response.ok) throw new Error("Failed to poll session events");
  const body = (await response.json()) as {
    events: Array<{
      id: number;
      type: string;
      data: Record<string, unknown> | null;
    }>;
    has_more: boolean;
  };
  return {
    events: body.events.map((event) => ({
      id: event.id,
      type: event.type,
      data: event.data ?? {},
    })),
    hasMore: body.has_more,
  };
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
  // 409 = already paused — treat as success.
  if (!response.ok && response.status !== 409) {
    throw new Error("Failed to pause session");
  }
}

export async function resumeSession(sessionId: string): Promise<void> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/resume`,
    { method: "POST" },
  );
  if (!response.ok) throw new Error("Failed to resume session");
}

/**
 * Retry a failed (or paused) session.  The backend emits
 * ``session.resume`` with ``source=user_retry``, flips the session
 * status to ``active``, and re-enqueues the session on the worker
 * queue.  The harness replays from the durable cursor so the last
 * user message is still in scope.
 */
export async function retrySession(sessionId: string): Promise<Session> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/retry`,
    { method: "POST" },
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Failed to retry session");
  }
  return (await response.json()) as Session;
}

/**
 * Stop a sub-agent / delegation child session.  Routes to the pause
 * endpoint today because pause already publishes the interrupt
 * signal and flips the child's status, but the UI vocabulary stays
 * "stop" so a future divergence (e.g. a terminal ``completed`` state
 * distinct from ``paused``) doesn't need a frontend rename.
 */
export async function stopSession(sessionId: string): Promise<void> {
  return pauseSession(sessionId);
}

export async function getBrowserState(
  sessionId: string,
): Promise<BrowserStateResponse | null> {
  const response = await authFetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/browser/state`,
  );
  if (response.status === 404) return null;
  if (!response.ok) throw new Error("Failed to fetch browser state");
  return (await response.json()) as BrowserStateResponse;
}

export async function getBrowserPreviewSnapshot(
  sessionId: string,
): Promise<Blob> {
  const response = await authFetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/browser/preview.png`,
  );
  if (response.status === 404) throw new Error("Browser preview is unavailable");
  if (!response.ok) throw new Error("Failed to fetch browser preview");
  return await response.blob();
}

export async function acquireBrowserControl(
  sessionId: string,
): Promise<BrowserControlResponse> {
  const response = await authFetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/browser/control`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "acquire" }),
    },
  );
  if (!response.ok) throw new Error("Failed to acquire browser control");
  return (await response.json()) as BrowserControlResponse;
}

export async function releaseBrowserControl(sessionId: string): Promise<void> {
  const response = await authFetch(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/browser/control`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "release" }),
    },
  );
  if (!response.ok) throw new Error("Failed to release browser control");
}

export async function getSessionTree(
  sessionId: string,
): Promise<SessionTreeResponse> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/tree`,
  );
  if (!response.ok) throw new Error("Failed to fetch session tree");
  return (await response.json()) as SessionTreeResponse;
}

export async function getSessionChildren(
  sessionId: string,
): Promise<SessionChildrenResponse> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/children`,
  );
  if (!response.ok) throw new Error("Failed to fetch session children");
  return (await response.json()) as SessionChildrenResponse;
}

export async function listScheduledWork(params?: {
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<ScheduledWorkListResponse> {
  const search = new URLSearchParams();
  if (params?.status) search.append("status", params.status);
  if (params?.limit != null) search.append("limit", String(params.limit));
  if (params?.offset != null) search.append("offset", String(params.offset));

  const response = await authFetch(`/api/v1/scheduled-work?${search}`);
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Failed to fetch scheduled work");
  }
  return (await response.json()) as ScheduledWorkListResponse;
}

export async function runScheduledWorkNow(
  scheduleId: string,
): Promise<{ id: string; queued: boolean }> {
  const response = await authFetch(
    `/api/v1/scheduled-work/${scheduleId}/run-now`,
    { method: "POST" },
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Failed to run scheduled work");
  }
  return (await response.json()) as { id: string; queued: boolean };
}

export async function cancelScheduledWork(scheduleId: string): Promise<void> {
  const response = await authFetch(`/api/v1/scheduled-work/${scheduleId}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Failed to cancel scheduled work");
  }
}

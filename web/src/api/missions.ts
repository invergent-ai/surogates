// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Raw fetch wrappers for the /v1/missions REST surface. The web app
// never calls these directly from React components — they are consumed
// only by the AgentChatAdapter implementation in
// `features/chat/surogates-web-chat-adapter.ts`, which converts wire
// shapes into the camelCase types declared in
// `@invergent/agent-chat-react`.
import { authFetch } from "./auth";

export interface MissionRow {
  id: string;
  org_id: string;
  user_id: string | null;
  service_account_id: string | null;
  session_id: string;
  agent_id: string;
  description: string;
  rubric: string;
  status: string;
  iteration: number;
  max_iterations: number;
  last_evaluation_result: string | null;
  last_evaluation_explanation: string | null;
  last_evaluation_feedback: string | null;
  last_evaluation_at: string | null;
  evaluator_parse_failures: number;
  paused_reason: string | null;
  cancelled_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface MissionTaskRow {
  id: string;
  goal: string;
  status: string;
  attempt_count: number;
  max_attempts: number;
  agent_def_name: string | null;
  result: string | null;
  result_metadata: Record<string, unknown> | null;
  parent_ids: string[];
  current_session_id: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface MissionWorkerRow {
  task_id: string;
  worker_session_id: string;
  agent_def_name: string | null;
  task_status: string;
  session_status: string;
  latest_event_id: number | null;
  latest_event_kind: string | null;
  latest_event_at: string | null;
  latest_event_summary: string | null;
  transcript_url: string;
}

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `mission API error (${response.status})`);
  }
  return (await response.json()) as T;
}

export async function listMissions(params?: {
  status?: string;
  agentId?: string;
}): Promise<{ missions: MissionRow[] }> {
  const search = new URLSearchParams();
  if (params?.status) search.set("status", params.status);
  if (params?.agentId) search.set("agent_id", params.agentId);
  const qs = search.toString();
  return readJson(
    await authFetch(`/api/v1/missions${qs ? `?${qs}` : ""}`),
  );
}

export async function getMission(missionId: string): Promise<MissionRow> {
  return readJson(
    await authFetch(`/api/v1/missions/${encodeURIComponent(missionId)}`),
  );
}

export async function getMissionTasks(
  missionId: string,
): Promise<{ tasks: MissionTaskRow[] }> {
  return readJson(
    await authFetch(
      `/api/v1/missions/${encodeURIComponent(missionId)}/tasks`,
    ),
  );
}

export async function getMissionWorkers(
  missionId: string,
): Promise<{ workers: MissionWorkerRow[] }> {
  return readJson(
    await authFetch(
      `/api/v1/missions/${encodeURIComponent(missionId)}/workers`,
    ),
  );
}

export async function pauseMission(
  missionId: string,
  reason?: string,
): Promise<void> {
  const response = await authFetch(
    `/api/v1/missions/${encodeURIComponent(missionId)}/pause`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ reason: reason ?? null }),
    },
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

export async function resumeMission(missionId: string): Promise<void> {
  const response = await authFetch(
    `/api/v1/missions/${encodeURIComponent(missionId)}/resume`,
    { method: "POST" },
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

export async function cancelMission(
  missionId: string,
  input: { reason?: string; cascadeToWorkers?: boolean },
): Promise<void> {
  const response = await authFetch(
    `/api/v1/missions/${encodeURIComponent(missionId)}/cancel`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        reason: input.reason ?? null,
        cascade_to_workers: input.cascadeToWorkers ?? false,
      }),
    },
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

export interface MissionEventRow {
  id: number;
  session_id: string;
  type: string;
  data: Record<string, unknown> | null;
  created_at: string | null;
}

export interface MissionEventsPage {
  events: MissionEventRow[];
  sessions: Record<
    string,
    {
      task_id: string | null;
      agent_def_name: string | null;
      kind: "coordinator" | "task" | "worker" | "delegation";
    }
  >;
}

export async function getMissionEvents(
  missionId: string,
  params?: { afterId?: number; limit?: number },
): Promise<MissionEventsPage> {
  const search = new URLSearchParams();
  if (params?.afterId !== undefined) {
    search.set("after_id", String(params.afterId));
  }
  if (params?.limit !== undefined) search.set("limit", String(params.limit));
  const qs = search.toString();
  return readJson(
    await authFetch(
      `/api/v1/missions/${encodeURIComponent(missionId)}/events${qs ? `?${qs}` : ""}`,
    ),
  );
}

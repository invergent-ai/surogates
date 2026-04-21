// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { authFetch } from "./auth";
import { parseError } from "./_errors";

export type AgentSource = "platform" | "org" | "user";

export interface AgentSummary {
  name: string;
  description: string;
  source: AgentSource;
  category: string | null;
  model: string | null;
  max_iterations: number | null;
  policy_profile: string | null;
  enabled: boolean;
}

export interface AgentListResponse {
  agents: AgentSummary[];
  total: number;
}

export interface AgentDetail {
  name: string;
  description: string;
  source: AgentSource;
  system_prompt: string;
  tools: string[] | null;
  disallowed_tools: string[] | null;
  model: string | null;
  max_iterations: number | null;
  policy_profile: string | null;
  category: string | null;
  tags: string[] | null;
  enabled: boolean;
}

export interface AgentActionResponse {
  success: boolean;
  message: string;
  category: string | null;
}

export async function listAgents(): Promise<AgentListResponse> {
  const response = await authFetch("/api/v1/agents");
  if (!response.ok) await parseError(response, "Failed to fetch sub-agents");
  return (await response.json()) as AgentListResponse;
}

export async function getAgent(name: string): Promise<AgentDetail> {
  const response = await authFetch(`/api/v1/agents/${encodeURIComponent(name)}`);
  if (!response.ok) await parseError(response, `Failed to fetch sub-agent '${name}'`);
  return (await response.json()) as AgentDetail;
}

export async function createAgent(body: {
  name: string;
  content: string;
  category?: string | null;
}): Promise<AgentActionResponse> {
  const response = await authFetch("/api/v1/agents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) await parseError(response, "Failed to create sub-agent");
  return (await response.json()) as AgentActionResponse;
}

export async function updateAgent(
  name: string,
  content: string,
): Promise<AgentActionResponse> {
  const response = await authFetch(`/api/v1/agents/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (!response.ok) await parseError(response, `Failed to update sub-agent '${name}'`);
  return (await response.json()) as AgentActionResponse;
}

export async function deleteAgent(name: string): Promise<void> {
  const response = await authFetch(`/api/v1/agents/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!response.ok && response.status !== 204) {
    await parseError(response, `Failed to delete sub-agent '${name}'`);
  }
}

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { authFetch } from "./auth";
import { parseError } from "./_errors";

export type SkillKind = "skill" | "expert";
export type SkillSource = "platform" | "org" | "user";
export type ExpertStatus = "draft" | "collecting" | "active" | "retired";

export interface SkillSummary {
  name: string;
  description: string;
  type: SkillKind;
  category: string | null;
  trigger: string | null;
  source: SkillSource;
  expert_status: ExpertStatus | null;
  expert_endpoint: string | null;
  expert_model: string | null;
}

export interface SkillListResponse {
  skills: SkillSummary[];
  total: number;
}

export interface SkillDetail {
  name: string;
  description: string;
  type: SkillKind;
  content: string;
  category: string | null;
  tags: string[] | null;
  trigger: string | null;
  source: SkillSource;
  linked_files: string[];
  staged_at: string | null;
  expert_model: string | null;
  expert_endpoint: string | null;
  expert_adapter: string | null;
  expert_status: ExpertStatus | null;
  expert_tools: string[] | null;
  expert_max_iterations: number | null;
  expert_stats: Record<string, unknown> | null;
}

export interface SkillActionResponse {
  success: boolean;
  message: string;
  category: string | null;
}

export async function listSkills(options?: {
  type?: SkillKind;
}): Promise<SkillListResponse> {
  const qs = options?.type ? `?type=${encodeURIComponent(options.type)}` : "";
  const response = await authFetch(`/api/v1/skills${qs}`);
  if (!response.ok) await parseError(response, "Failed to fetch skills");
  return (await response.json()) as SkillListResponse;
}

export async function getSkill(name: string): Promise<SkillDetail> {
  const response = await authFetch(`/api/v1/skills/${encodeURIComponent(name)}`);
  if (!response.ok) await parseError(response, `Failed to fetch skill '${name}'`);
  return (await response.json()) as SkillDetail;
}

export async function createSkill(body: {
  name: string;
  content: string;
  category?: string | null;
}): Promise<SkillActionResponse> {
  const response = await authFetch("/api/v1/skills", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) await parseError(response, "Failed to create skill");
  return (await response.json()) as SkillActionResponse;
}

export async function updateSkill(
  name: string,
  content: string,
): Promise<SkillActionResponse> {
  const response = await authFetch(`/api/v1/skills/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (!response.ok) await parseError(response, `Failed to update skill '${name}'`);
  return (await response.json()) as SkillActionResponse;
}

export async function deleteSkill(name: string): Promise<void> {
  const response = await authFetch(`/api/v1/skills/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!response.ok && response.status !== 204) {
    await parseError(response, `Failed to delete skill '${name}'`);
  }
}

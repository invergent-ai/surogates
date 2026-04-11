// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { authFetch } from "./auth";

export interface SkillSummary {
  name: string;
  description: string;
  category: string | null;
  trigger: string | null;
}

export interface SkillListResponse {
  skills: SkillSummary[];
  total: number;
}

export async function listSkills(): Promise<SkillListResponse> {
  const response = await authFetch("/api/v1/skills");
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Failed to fetch skills");
  }
  return (await response.json()) as SkillListResponse;
}

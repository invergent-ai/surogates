// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { authFetch } from "./auth";
import type {
  ArtifactMeta,
  ArtifactPayload,
} from "@/types/session";

interface ArtifactListResponse {
  artifacts: ArtifactMeta[];
}

export async function listArtifacts(
  sessionId: string,
): Promise<ArtifactMeta[]> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/artifacts`,
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Failed to list artifacts");
  }
  const data = (await response.json()) as ArtifactListResponse;
  return data.artifacts;
}

export async function getArtifact(
  sessionId: string,
  artifactId: string,
): Promise<ArtifactPayload> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/artifacts/${artifactId}`,
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Failed to fetch artifact");
  }
  return (await response.json()) as ArtifactPayload;
}

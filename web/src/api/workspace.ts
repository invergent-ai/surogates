// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { authFetch } from "./auth";

export interface FileEntry {
  name: string;
  path: string;
  kind: "file" | "dir";
  size?: number;
  children?: FileEntry[];
}

export interface WorkspaceTreeResponse {
  root: string;
  entries: FileEntry[];
  truncated: boolean;
}

export interface FileContentResponse {
  path: string;
  content: string;
  size: number;
  mime_type: string | null;
  /** "utf-8" for text files, "base64" for images. */
  encoding: "utf-8" | "base64";
  truncated: boolean;
}

export async function getWorkspaceTree(
  sessionId: string,
): Promise<WorkspaceTreeResponse> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/workspace/tree`,
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Failed to fetch workspace tree");
  }
  return (await response.json()) as WorkspaceTreeResponse;
}

export interface Checkpoint {
  hash: string;
  short_hash: string;
  timestamp: string;
  reason: string;
  files_changed: number;
  insertions: number;
  deletions: number;
}

export interface CheckpointListResponse {
  checkpoints: Checkpoint[];
}

export interface RollbackResponse {
  success: boolean;
  restored_to: string | null;
  reason: string | null;
  error: string | null;
}

export async function listCheckpoints(
  sessionId: string,
): Promise<CheckpointListResponse> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/workspace/checkpoints`,
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Failed to fetch checkpoints");
  }
  return (await response.json()) as CheckpointListResponse;
}

export async function rollbackToCheckpoint(
  sessionId: string,
  checkpointHash: string,
  filePath?: string,
): Promise<RollbackResponse> {
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/workspace/rollback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        checkpoint_hash: checkpointHash,
        file_path: filePath,
      }),
    },
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Rollback failed");
  }
  return (await response.json()) as RollbackResponse;
}

export interface UploadResponse {
  path: string;
  size: number;
}

export async function uploadFile(
  sessionId: string,
  file: File,
  directory?: string,
): Promise<UploadResponse> {
  const params = new URLSearchParams();
  if (directory) params.append("path", directory);

  const formData = new FormData();
  formData.append("file", file);

  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/workspace/upload?${params}`,
    { method: "POST", body: formData },
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Upload failed");
  }
  return (await response.json()) as UploadResponse;
}

export function getDownloadUrl(sessionId: string, path: string): string {
  const params = new URLSearchParams({ path });
  return `/api/v1/sessions/${sessionId}/workspace/download?${params}`;
}

export async function deleteFile(
  sessionId: string,
  path: string,
): Promise<void> {
  const params = new URLSearchParams({ path });
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/workspace/file?${params}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Delete failed");
  }
}

export async function getWorkspaceFile(
  sessionId: string,
  path: string,
): Promise<FileContentResponse> {
  const params = new URLSearchParams({ path });
  const response = await authFetch(
    `/api/v1/sessions/${sessionId}/workspace/file?${params}`,
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(err?.detail ?? "Failed to fetch file content");
  }
  return (await response.json()) as FileContentResponse;
}

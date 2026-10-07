// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { authFetch } from "./auth";
import { errorDetailMessage } from "./_errors";
import { untilAnswered } from "./device-requests";
import { getSession } from "./sessions";

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
  /** "utf-8" for text files, "base64" for inline binary previews. */
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
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Failed to fetch workspace tree");
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
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Failed to fetch checkpoints");
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
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Rollback failed");
  }
  return (await response.json()) as RollbackResponse;
}

export interface UploadResponse {
  path: string;
  size: number;
}

// Whether a chat works on a folder of its user's computer, by session: a chat's place never changes.
const onDevice = new Map<string, Promise<boolean>>();

function isOnDevice(sessionId: string): Promise<boolean> {
  let known = onDevice.get(sessionId);
  if (known === undefined) {
    known = getSession(sessionId).then(
      (session) => (session.config?.execution as { kind?: unknown } | undefined)?.kind === "device",
    );
    // Not known after all: asked again next time.
    known.catch(() => onDevice.delete(sessionId));
    onDevice.set(sessionId, known);
  }
  return known;
}

export async function uploadFile(
  sessionId: string,
  file: File,
  directory?: string,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  const params = new URLSearchParams();
  if (directory) params.append("path", directory);

  const formData = new FormData();
  formData.append("file", file);

  const response = await untilAnswered(
    // The panel's signal stops only a local-folder chat's sending: untilAnswered hands it on for those alone.
    (requestId, change, sending) =>
      authFetch(
        `/api/v1/sessions/${sessionId}/workspace/upload?${new URLSearchParams([...params, ["request_id", requestId]])}`,
        // Sent again by its change, the file does not cross again.
        change === null
          ? { method: "POST", body: formData, signal: sending }
          : { method: "POST", headers: { "X-Change-Digest": change }, signal: sending },
      ),
    { onDevice: await isOnDevice(sessionId), signal },
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Upload failed");
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
  signal?: AbortSignal,
): Promise<void> {
  const response = await untilAnswered(
    (requestId, _change, sending) =>
      authFetch(
        `/api/v1/sessions/${sessionId}/workspace/file?${new URLSearchParams({ path, request_id: requestId })}`,
        { method: "DELETE", signal: sending },
      ),
    { onDevice: await isOnDevice(sessionId), signal },
  );
  if (!response.ok) {
    const err = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Delete failed");
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
      detail?: unknown;
    } | null;
    throw new Error(errorDetailMessage(err?.detail) ?? "Failed to fetch file content");
  }
  return (await response.json()) as FileContentResponse;
}

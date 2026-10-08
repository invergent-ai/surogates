// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { authFetch } from "./auth";
import { errorDetailMessage } from "./_errors";
import { computerOf, onDeviceOf, refusalOf, untilAnswered, untilOnline } from "./device-requests";
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

/**
 * A chat's file tree. Where its caller waits (*onWaiting*), a local-folder chat's tree is read again
 * while its computer is offline, said "Waiting for <computer>", until it answers or *signal* stops it.
 */
export async function getWorkspaceTree(
  sessionId: string,
  { signal, onWaiting }: { signal?: AbortSignal; onWaiting?: (said: string) => void } = {},
): Promise<WorkspaceTreeResponse> {
  const read = (reading: AbortSignal | undefined) =>
    authFetch(`/api/v1/sessions/${sessionId}/workspace/tree`, { signal: reading });
  const response = onWaiting
    ? await untilOnline(read, {
        signal,
        onWaiting: () => {
          computerFor(sessionId).then((computer) => onWaiting(`Waiting for ${computer}`));
        },
      })
    : await read(signal);
  if (!response.ok) {
    throw new Error(await refusal(sessionId, response, "Failed to fetch workspace tree"));
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

// Where a chat works, by session: on a folder of its user's computer, and which, or in the cloud. A
// chat's place never changes.
const places = new Map<string, Promise<{ onDevice: boolean; computer: string }>>();

function placeOf(sessionId: string): Promise<{ onDevice: boolean; computer: string }> {
  let known = places.get(sessionId);
  if (known === undefined) {
    known = getSession(sessionId).then((session) => ({ onDevice: onDeviceOf(session.config), computer: computerOf(session.config) }));
    // Not known after all: asked again next time.
    known.catch(() => places.delete(sessionId));
    places.set(sessionId, known);
  }
  return known;
}

// The computer *sessionId*'s folder is on, by name; the user's computer where the chat cannot be read.
const computerFor = (sessionId: string): Promise<string> =>
  placeOf(sessionId).then(({ computer }) => computer, () => "your computer");

// Why *response* refused a request about *sessionId*'s files: a computer whose access ended or that
// is offline as the file panel says them, else the server's own words, else *fallback*.
async function refusal(sessionId: string, response: Response, fallback: string): Promise<string> {
  const err = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  return (await refusalOf(err?.detail, () => computerFor(sessionId))) ?? errorDetailMessage(err?.detail) ?? fallback;
}

// What a change waiting on its computer says while it is sent again: for its user's answer there, or its turn.
const waitingFor = (computer: string, status: 202 | 429): string =>
  status === 202 ? `Waiting for you to allow this on ${computer}` : `Waiting for ${computer}`;

export async function uploadFile(
  sessionId: string,
  file: File,
  directory?: string,
  signal?: AbortSignal,
  onWaiting?: (said: string) => void,
): Promise<UploadResponse> {
  const params = new URLSearchParams();
  if (directory) params.append("path", directory);

  const formData = new FormData();
  formData.append("file", file);
  const place = await placeOf(sessionId);

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
    { onDevice: place.onDevice, signal, onWaiting: (status) => onWaiting?.(waitingFor(place.computer, status)) },
  );
  if (!response.ok) {
    throw new Error(await refusal(sessionId, response, "Upload failed"));
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
  onWaiting?: (said: string) => void,
): Promise<void> {
  const place = await placeOf(sessionId);
  const response = await untilAnswered(
    (requestId, _change, sending) =>
      authFetch(
        `/api/v1/sessions/${sessionId}/workspace/file?${new URLSearchParams({ path, request_id: requestId })}`,
        { method: "DELETE", signal: sending },
      ),
    { onDevice: place.onDevice, signal, onWaiting: (status) => onWaiting?.(waitingFor(place.computer, status)) },
  );
  if (!response.ok) {
    throw new Error(await refusal(sessionId, response, "Delete failed"));
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
    throw new Error(await refusal(sessionId, response, "Failed to fetch file content"));
  }
  return (await response.json()) as FileContentResponse;
}

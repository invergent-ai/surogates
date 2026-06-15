// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import type {
  AgentChatImageAttachment,
  AgentChatPendingAttachment,
} from "../types";

/**
 * Minimal shape of a composer file part needed to split it for sending.
 * Mirrors the relevant fields of ``PromptInputFileUIPart`` without
 * coupling this pure helper to the prompt-input module.
 */
export interface ComposerFilePart {
  /** MIME type, e.g. "image/png". */
  mediaType?: string;
  /** Data URL (base64) for images; blob/HTTP URL otherwise. */
  url?: string;
  filename?: string;
  /** Backing File, retained so the runtime can upload to the workspace. */
  file?: File;
}

export interface SplitComposerFiles {
  images: AgentChatImageAttachment[];
  pending: AgentChatPendingAttachment[];
}

/**
 * Split composer files into the inline-vision image list and the
 * workspace-upload pending list.
 *
 * Images go to **both** buckets: the base64 ``url`` feeds the inline
 * vision path so vision-capable models see the image on this turn, and —
 * when the part carries its backing ``File`` — the same image is also
 * queued as a pending attachment so the runtime persists it to the
 * workspace ``uploads/`` directory, exactly like a non-image file.
 * Non-image files only ever become pending attachments.
 *
 * A FileUIPart with no backing ``File`` (e.g. an inline image synthesised
 * from an HTTP source) is still surfaced inline when it has a ``url`` but
 * cannot be uploaded, so it is omitted from the pending list rather than
 * pretending we can persist nothing.
 */
export function splitComposerFiles(
  files: readonly ComposerFilePart[],
): SplitComposerFiles {
  const images: AgentChatImageAttachment[] = [];
  const pending: AgentChatPendingAttachment[] = [];

  for (const file of files) {
    if (file.mediaType?.startsWith("image/") && file.url) {
      images.push({ data: file.url, mimeType: file.mediaType });
    }
    if (!file.file) {
      continue;
    }
    pending.push({
      file: file.file,
      filename: file.filename ?? file.file.name,
      mimeType: file.mediaType ?? file.file.type ?? undefined,
      size: file.file.size,
    });
  }

  return { images, pending };
}
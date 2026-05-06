// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Per-kind serialisers for copy-to-clipboard and download actions in
// the artifact header.  Each kind returns a ``{text, mime, extension}``
// triple so both actions reuse the same canonical serialisation.

import type { ArtifactPayload } from "../../../types";

export interface ArtifactExport {
  text: string;
  mime: string;
  extension: string;
}

export function exportArtifact(payload: ArtifactPayload): ArtifactExport {
  switch (payload.kind) {
    case "markdown":
      return {
        text: payload.spec.content,
        mime: "text/markdown",
        extension: "md",
      };
    case "table":
      return {
        text: tableToCsv(payload.spec.columns, payload.spec.rows),
        mime: "text/csv",
        extension: "csv",
      };
    case "chart":
      return {
        text: JSON.stringify(payload.spec.vega_lite, null, 2),
        mime: "application/json",
        extension: "json",
      };
    case "html":
      return {
        text: payload.spec.html,
        mime: "text/html",
        extension: "html",
      };
    case "svg":
      return {
        text: payload.spec.svg,
        mime: "image/svg+xml",
        extension: "svg",
      };
  }
}

function tableToCsv(
  columns: string[],
  rows: Array<Record<string, unknown>>,
): string {
  const lines: string[] = [];
  lines.push(columns.map(csvCell).join(","));
  for (const row of rows) {
    lines.push(columns.map((c) => csvCell(row[c])).join(","));
  }
  return lines.join("\n");
}

function csvCell(value: unknown): string {
  if (value == null) return "";
  const str =
    typeof value === "string" ? value : JSON.stringify(value) ?? String(value);
  // RFC 4180: escape cells containing commas, quotes, or newlines by
  // wrapping in double quotes and doubling any internal quotes.
  if (/[",\n\r]/.test(str)) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

/**
 * Build a safe filename slug from the artifact's user-supplied name.
 * Keeps letters, numbers, dashes, underscores, dots; collapses
 * everything else to ``-``.
 */
export function safeFilename(name: string): string {
  const slug = name
    .trim()
    .replace(/[^\w.\-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "artifact";
}

/** Download a string payload as a file with the given name + mime. */
export function downloadText(filename: string, text: string, mime: string): void {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Revoke after the click so Safari has time to initiate the download.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Copy text to the clipboard.  Returns true on success. */
export async function copyText(text: string): Promise<boolean> {
  if (typeof window === "undefined" || !navigator?.clipboard?.writeText) {
    return false;
  }
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

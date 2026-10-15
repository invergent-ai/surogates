// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// WorkspaceFileCard — one file of a turn's downloads, laid out as Claude
// lays its file cards: the file's icon, its name over "Kind · EXT", and a
// Download button at the end. Clicking the card body opens the file in the
// same right-column viewer the workspace uses — one viewer in the product,
// never a second dialog-shaped one. The Download button triggers a
// same-origin download via the adapter's getWorkspaceDownloadUrl.

import { useCallback } from "react";
import {
  CodeIcon,
  FileArchiveIcon,
  FileAudioIcon,
  FileChartLineIcon,
  FileCodeIcon,
  FileIcon,
  FileImageIcon,
  FileSpreadsheetIcon,
  FileTextIcon,
  FileVideoIcon,
  type LucideIcon,
} from "lucide-react";

import { useAgentChatAdapterContext } from "../../adapter-context";
import { cn } from "../../lib/utils";

export interface WorkspaceFileCardProps {
  sessionId: string;
  /** Workspace-relative path returned by the harness. */
  path: string;
  /** Display label — typically the file basename, but the summarizer
   *  is free to supply any human-readable name. */
  label: string;
}

interface FileKindInfo {
  icon: LucideIcon;
  /** What the file is, ahead of its extension ("Document · DOCX"); plain
   *  text has none and reads as its extension alone. */
  kind?: string;
}

const DOCUMENT = { icon: FileTextIcon, kind: "Document" };
const SPREADSHEET = { icon: FileSpreadsheetIcon, kind: "Spreadsheet" };
const PRESENTATION = { icon: FileChartLineIcon, kind: "Presentation" };
const IMAGE = { icon: FileImageIcon, kind: "Image" };
const AUDIO = { icon: FileAudioIcon, kind: "Audio" };
const VIDEO = { icon: FileVideoIcon, kind: "Video" };
const ARCHIVE = { icon: FileArchiveIcon, kind: "Archive" };
const CODE = { icon: FileCodeIcon, kind: "Code" };

const _EXTENSION_KIND: Record<string, FileKindInfo> = {
  pdf: DOCUMENT, doc: DOCUMENT, docx: DOCUMENT, odt: DOCUMENT, rtf: DOCUMENT, md: DOCUMENT,
  txt: { icon: FileTextIcon },
  xls: SPREADSHEET, xlsx: SPREADSHEET, ods: SPREADSHEET, csv: SPREADSHEET, tsv: SPREADSHEET,
  ppt: PRESENTATION, pptx: PRESENTATION, odp: PRESENTATION,
  png: IMAGE, jpg: IMAGE, jpeg: IMAGE, gif: IMAGE, svg: IMAGE, webp: IMAGE, bmp: IMAGE,
  mp3: AUDIO, wav: AUDIO, ogg: AUDIO,
  mp4: VIDEO, mov: VIDEO, webm: VIDEO,
  zip: ARCHIVE, tar: ARCHIVE, gz: ARCHIVE, bz2: ARCHIVE, "7z": ARCHIVE,
  py: CODE, js: CODE, ts: CODE, tsx: CODE, jsx: CODE, json: CODE, yaml: CODE, yml: CODE,
  toml: CODE, sh: { icon: CodeIcon, kind: "Code" }, rs: CODE, go: CODE, rb: CODE, java: CODE,
  cpp: CODE, c: CODE, html: CODE, css: CODE, scss: CODE, sql: CODE,
};

function _extension(path: string): string {
  const idx = path.lastIndexOf(".");
  if (idx < 0 || idx === path.length - 1) return "";
  return path.slice(idx + 1).toLowerCase();
}

function _basename(path: string): string {
  const idx = path.lastIndexOf("/");
  return idx >= 0 ? path.slice(idx + 1) : path;
}

/** The name a downloaded file is saved under: the path's own, else the label. */
function downloadNameFor(path: string, label: string): string {
  return _basename(path) || label;
}

export function WorkspaceFileCard({
  sessionId,
  path,
  label,
}: WorkspaceFileCardProps) {
  const { adapter, onFileSelect } = useAgentChatAdapterContext();
  const ext = _extension(path);
  const { icon: Icon, kind } = _EXTENSION_KIND[ext] ?? { icon: FileIcon };
  const downloadUrl = adapter.getWorkspaceDownloadUrl({ sessionId, path });
  const downloadName = downloadNameFor(path, label);
  // The file's own name — never the folders above it — without the
  // extension, which the line below states instead.
  const name = _basename(label) || label;
  const title = ext && name.toLowerCase().endsWith(`.${ext}`)
    ? name.slice(0, -(ext.length + 1))
    : name;
  const subtitle = ext
    ? [kind, ext.toUpperCase()].filter(Boolean).join(" · ")
    : "File";

  const handleCardClick = useCallback(() => {
    onFileSelect?.(path);
  }, [onFileSelect, path]);

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={handleCardClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handleCardClick();
        }
      }}
      className={cn(
        "group/file flex items-center gap-5 rounded-xl border border-border",
        "bg-muted/60 py-3.5 pr-4 pl-5 text-left transition-colors",
        "cursor-pointer hover:bg-muted",
      )}
    >
      <div className="flex size-10 shrink-0 items-center justify-center rounded-md border border-border bg-background">
        <Icon className="size-5 text-muted-foreground" aria-hidden />
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm text-foreground">{title}</div>
        <div className="truncate text-xs text-muted-foreground">{subtitle}</div>
      </div>
      <a
        href={downloadUrl}
        download={downloadName}
        // Stop the click from opening the pane viewer.
        onClick={(e) => e.stopPropagation()}
        className="inline-flex h-8 shrink-0 items-center rounded-md bg-accent px-3 text-sm text-foreground transition-colors hover:bg-foreground/15"
        aria-label={`Download ${downloadName}`}
      >
        Download
      </a>
    </div>
  );
}

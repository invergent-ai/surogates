// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// WorkspaceFileCard — Claude-style downloadable card rendered for
// each file artifact in the TurnSummaryCard. Clicking the card body
// opens the file in the same right-column viewer the workspace uses —
// one viewer in the product, never a second dialog-shaped one.
// Clicking the Download button triggers a same-origin download via
// the adapter's getWorkspaceDownloadUrl.

import { useCallback } from "react";
import {
  CodeIcon,
  DownloadIcon,
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
  typeLabel: string;
}

const _EXTENSION_KIND: Record<string, FileKindInfo> = {
  // Documents
  pdf: { icon: FileTextIcon, typeLabel: "PDF" },
  doc: { icon: FileTextIcon, typeLabel: "Word document" },
  docx: { icon: FileTextIcon, typeLabel: "Word document" },
  odt: { icon: FileTextIcon, typeLabel: "OpenDocument text" },
  rtf: { icon: FileTextIcon, typeLabel: "Rich text" },
  txt: { icon: FileTextIcon, typeLabel: "Text" },
  md: { icon: FileTextIcon, typeLabel: "Markdown" },
  // Spreadsheets
  xls: { icon: FileSpreadsheetIcon, typeLabel: "Spreadsheet" },
  xlsx: { icon: FileSpreadsheetIcon, typeLabel: "Spreadsheet" },
  ods: { icon: FileSpreadsheetIcon, typeLabel: "OpenDocument spreadsheet" },
  csv: { icon: FileSpreadsheetIcon, typeLabel: "CSV" },
  tsv: { icon: FileSpreadsheetIcon, typeLabel: "TSV" },
  // Presentations / charts
  ppt: { icon: FileChartLineIcon, typeLabel: "Presentation" },
  pptx: { icon: FileChartLineIcon, typeLabel: "Presentation" },
  // Images
  png: { icon: FileImageIcon, typeLabel: "Image" },
  jpg: { icon: FileImageIcon, typeLabel: "Image" },
  jpeg: { icon: FileImageIcon, typeLabel: "Image" },
  gif: { icon: FileImageIcon, typeLabel: "Image" },
  svg: { icon: FileImageIcon, typeLabel: "SVG image" },
  webp: { icon: FileImageIcon, typeLabel: "Image" },
  bmp: { icon: FileImageIcon, typeLabel: "Image" },
  // Audio / video
  mp3: { icon: FileAudioIcon, typeLabel: "Audio" },
  wav: { icon: FileAudioIcon, typeLabel: "Audio" },
  ogg: { icon: FileAudioIcon, typeLabel: "Audio" },
  mp4: { icon: FileVideoIcon, typeLabel: "Video" },
  mov: { icon: FileVideoIcon, typeLabel: "Video" },
  webm: { icon: FileVideoIcon, typeLabel: "Video" },
  // Archives
  zip: { icon: FileArchiveIcon, typeLabel: "Archive" },
  tar: { icon: FileArchiveIcon, typeLabel: "Archive" },
  gz: { icon: FileArchiveIcon, typeLabel: "Archive" },
  bz2: { icon: FileArchiveIcon, typeLabel: "Archive" },
  "7z": { icon: FileArchiveIcon, typeLabel: "Archive" },
  // Code
  py: { icon: FileCodeIcon, typeLabel: "Python" },
  js: { icon: FileCodeIcon, typeLabel: "JavaScript" },
  ts: { icon: FileCodeIcon, typeLabel: "TypeScript" },
  tsx: { icon: FileCodeIcon, typeLabel: "TypeScript" },
  jsx: { icon: FileCodeIcon, typeLabel: "JavaScript" },
  json: { icon: FileCodeIcon, typeLabel: "JSON" },
  yaml: { icon: FileCodeIcon, typeLabel: "YAML" },
  yml: { icon: FileCodeIcon, typeLabel: "YAML" },
  toml: { icon: FileCodeIcon, typeLabel: "TOML" },
  sh: { icon: CodeIcon, typeLabel: "Shell" },
  rs: { icon: FileCodeIcon, typeLabel: "Rust" },
  go: { icon: FileCodeIcon, typeLabel: "Go" },
  rb: { icon: FileCodeIcon, typeLabel: "Ruby" },
  java: { icon: FileCodeIcon, typeLabel: "Java" },
  cpp: { icon: FileCodeIcon, typeLabel: "C++" },
  c: { icon: FileCodeIcon, typeLabel: "C" },
  html: { icon: FileCodeIcon, typeLabel: "HTML" },
  css: { icon: FileCodeIcon, typeLabel: "CSS" },
  scss: { icon: FileCodeIcon, typeLabel: "Sass" },
  sql: { icon: FileCodeIcon, typeLabel: "SQL" },
};

function _extension(path: string): string {
  const idx = path.lastIndexOf(".");
  if (idx < 0 || idx === path.length - 1) return "";
  return path.slice(idx + 1).toLowerCase();
}

function _fileKindFor(path: string): FileKindInfo {
  const ext = _extension(path);
  return _EXTENSION_KIND[ext] ?? { icon: FileIcon, typeLabel: "File" };
}

function _basename(path: string): string {
  const idx = path.lastIndexOf("/");
  return idx >= 0 ? path.slice(idx + 1) : path;
}

export function WorkspaceFileCard({
  sessionId,
  path,
  label,
}: WorkspaceFileCardProps) {
  const { adapter, onFileSelect } = useAgentChatAdapterContext();
  const kind = _fileKindFor(path);
  const Icon = kind.icon;
  const downloadUrl = adapter.getWorkspaceDownloadUrl({ sessionId, path });
  const downloadName = _basename(path) || label;

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
        "group/file flex items-center gap-3 rounded-lg border border-border",
        "bg-muted/20 px-3 py-2 text-left transition-colors",
        "cursor-pointer hover:bg-muted/40",
      )}
    >
      <div className="flex size-10 shrink-0 items-center justify-center rounded-md border border-border bg-background">
        <Icon className="size-5 text-muted-foreground" aria-hidden />
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-foreground">
          {label}
        </div>
        <div className="truncate text-xs text-muted-foreground">
          {kind.typeLabel}
        </div>
      </div>
      <a
        href={downloadUrl}
        download={downloadName}
        // Stop the click from opening the pane viewer.
        onClick={(e) => e.stopPropagation()}
        className={cn(
          "shrink-0 inline-flex items-center gap-1.5 rounded-md border border-border",
          "bg-background px-2.5 py-1.5 text-xs font-medium text-foreground",
          "transition-colors hover:bg-muted",
        )}
        aria-label={`Download ${downloadName}`}
      >
        <DownloadIcon className="size-4" aria-hidden />
        Download
      </a>
    </div>
  );
}

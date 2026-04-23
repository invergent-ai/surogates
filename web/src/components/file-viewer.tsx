// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Inline file viewer — renders in the bottom half of the workspace panel.
//
import { useAppStore } from "@/stores/app-store";
import { formatFileSize, getLanguageHint } from "@/lib/format";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertCircleIcon, FileTextIcon, ImageIcon, XIcon } from "lucide-react";

const SKELETON_WIDTHS = [70, 85, 55, 90, 60, 78, 45, 82, 65, 72];

export function FileViewer() {
  const file = useAppStore((s) => s.workspaceFile);
  const loading = useAppStore((s) => s.workspaceFileLoading);
  const error = useAppStore((s) => s.workspaceFileError);
  const clearFile = useAppStore((s) => s.clearWorkspaceFile);

  const visible = loading || file !== null || error !== null;
  if (!visible) return null;

  const fileName = file?.path.split("/").pop() ?? "File";
  const lang = file ? getLanguageHint(file.path) : "";
  const isImage = file?.encoding === "base64";
  const HeaderIcon = isImage ? ImageIcon : FileTextIcon;

  return (
    <div className="flex flex-col border-t border-line min-h-0 flex-1">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-line shrink-0">
        <HeaderIcon className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="text-xs font-medium truncate">{fileName}</div>
          <div className="text-[10px] text-muted-foreground truncate">
            {file?.path ?? "Loading..."}
            {file && (
              <span className="ml-1.5">
                {formatFileSize(file.size)}
                {file.truncated && " (truncated)"}
              </span>
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={clearFile}
          className="p-0.5 rounded hover:bg-muted text-muted-foreground hover:text-foreground transition-colors shrink-0"
          aria-label="Close file"
        >
          <XIcon className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Content */}
      <ScrollArea className="flex-1 min-h-0">
        {loading && (
          <div className="p-3 space-y-1.5">
            {Array.from({ length: 10 }).map((_, i) => (
              <Skeleton
                key={i}
                className="h-3.5 rounded"
                style={{ width: `${SKELETON_WIDTHS[i % SKELETON_WIDTHS.length]}%` }}
              />
            ))}
          </div>
        )}

        {error && (
          <div className="flex items-start gap-2 p-3 text-xs text-destructive">
            <AlertCircleIcon className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {file && isImage && (
          <div className="flex items-center justify-center p-4">
            <img
              src={`data:${file.mime_type ?? "image/png"};base64,${file.content}`}
              alt={fileName}
              className="max-w-full max-h-[60vh] object-contain rounded"
            />
          </div>
        )}

        {file && !isImage && (
          <pre className="p-3 text-[11px] leading-relaxed  text-foreground whitespace-pre-wrap wrap-break-word">
            <code data-language={lang}>{file.content}</code>
          </pre>
        )}
      </ScrollArea>
    </div>
  );
}

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useAppStore } from "@/stores/app-store";
import { formatFileSize, getLanguageHint } from "@/lib/format";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertCircleIcon, FileTextIcon } from "lucide-react";

const SKELETON_WIDTHS = [70, 85, 55, 90, 60, 78, 45, 82, 65, 72, 88, 50];

export function FileViewer() {
  const file = useAppStore((s) => s.workspaceFile);
  const loading = useAppStore((s) => s.workspaceFileLoading);
  const error = useAppStore((s) => s.workspaceFileError);
  const clearFile = useAppStore((s) => s.clearWorkspaceFile);

  const open = loading || file !== null || error !== null;

  function handleOpenChange(next: boolean) {
    if (!next) clearFile();
  }

  const fileName = file?.path.split("/").pop() ?? "File";
  const lang = file ? getLanguageHint(file.path) : "";

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-4xl h-[80vh] flex flex-col p-0 gap-0">
        <DialogHeader className="px-5 py-4 border-b border-line shrink-0">
          <div className="flex items-center gap-2">
            <FileTextIcon className="w-4 h-4 text-muted-foreground shrink-0" />
            <div className="min-w-0">
              <DialogTitle className="text-sm font-medium truncate">
                {fileName}
              </DialogTitle>
              <DialogDescription className="text-xs text-faint truncate">
                {file?.path ?? "Loading..."}
                {file && (
                  <span className="ml-2">
                    {formatFileSize(file.size)}
                    {file.truncated && " (truncated)"}
                  </span>
                )}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <ScrollArea className="flex-1 overflow-hidden">
          {loading && (
            <div className="p-5 space-y-2">
              {Array.from({ length: 12 }).map((_, i) => (
                <Skeleton
                  key={i}
                  className="h-4 rounded"
                  style={{ width: `${SKELETON_WIDTHS[i % SKELETON_WIDTHS.length]}%` }}
                />
              ))}
            </div>
          )}

          {error && (
            <div className="flex items-start gap-2 p-5 text-sm text-destructive">
              <AlertCircleIcon className="w-4 h-4 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          {file && (
            <pre className="p-5 text-xs leading-relaxed font-mono text-foreground whitespace-pre-wrap break-words">
              <code data-language={lang}>{file.content}</code>
            </pre>
          )}
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}

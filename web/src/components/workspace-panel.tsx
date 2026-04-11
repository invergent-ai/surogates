// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useEffect, useState, useCallback, useRef } from "react";
import {
  FolderOpenIcon,
  RefreshCwIcon,
  AlertCircleIcon,
  UploadIcon,
  Loader2Icon,
  DownloadIcon,
  TrashIcon,
} from "lucide-react";
import { toast } from "sonner";
import {
  FileTree,
  FileTreeFile,
  FileTreeFolder,
} from "@/components/ai-elements/file-tree";
import { useAppStore } from "@/stores/app-store";
import { cn } from "@/lib/utils";
import { formatFileSize } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { FileViewer } from "@/components/file-viewer";
import * as workspaceApi from "@/api/workspace";
import type { FileEntry } from "@/api/workspace";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const TEXT_EXTENSIONS = new Set([
  ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".yaml", ".yml",
  ".toml", ".sql", ".sh", ".css", ".html", ".txt", ".csv", ".rst",
  ".cfg", ".ini", ".env", ".rs", ".go", ".java", ".rb", ".php",
  ".c", ".cpp", ".h", ".lock", ".gitignore", ".dockerignore", ".editorconfig",
]);

const SKELETON_WIDTHS = [75, 60, 90, 65, 80, 70, 85, 55];

function isTextLike(name: string): boolean {
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot).toLowerCase() : "";
  return TEXT_EXTENSIONS.has(ext);
}

// Collect all directory paths for default expansion (depth < 2).
function collectExpandedPaths(entries: FileEntry[], depth = 0): string[] {
  const paths: string[] = [];
  for (const entry of entries) {
    if (entry.kind === "dir" && depth < 1) {
      paths.push(entry.path);
      if (entry.children) {
        paths.push(...collectExpandedPaths(entry.children, depth + 1));
      }
    }
  }
  return paths;
}

// ---------------------------------------------------------------------------
// Recursive tree renderer
// ---------------------------------------------------------------------------

function RenderEntries({
  entries,
  sessionId,
  onFileSelect,
  onDelete,
}: {
  entries: FileEntry[];
  sessionId: string;
  onFileSelect: (path: string) => void;
  onDelete: (path: string) => void;
}) {
  return (
    <>
      {entries.map((entry) => {
        if (entry.kind === "dir") {
          return (
            <FileTreeFolder key={entry.path} name={entry.name} path={entry.path}>
              {entry.children && entry.children.length > 0 && (
                <RenderEntries
                  entries={entry.children}
                  sessionId={sessionId}
                  onFileSelect={onFileSelect}
                  onDelete={onDelete}
                />
              )}
            </FileTreeFolder>
          );
        }

        const textLike = isTextLike(entry.name);

        return (
          <FileTreeFile
            key={entry.path}
            name={entry.name}
            path={entry.path}
          >
            <span className="size-4 shrink-0" />
            <span className="truncate flex-1">{entry.name}</span>
            {entry.size != null && (
              <span className="text-xs text-muted-foreground/60 shrink-0 ml-1">
                {formatFileSize(entry.size)}
              </span>
            )}
            <div className="flex items-center gap-0 opacity-0 group-hover:opacity-100 transition-opacity shrink-0 ml-1">
              {textLike && (
                <button
                  type="button"
                  className="p-0.5 rounded hover:bg-muted text-muted-foreground hover:text-foreground"
                  onClick={(e) => {
                    e.stopPropagation();
                    onFileSelect(entry.path);
                  }}
                  title="View"
                >
                  <DownloadIcon className="size-3" />
                </button>
              )}
              <button
                type="button"
                className="p-0.5 rounded hover:bg-destructive/10 text-muted-foreground hover:text-destructive"
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(entry.path);
                }}
                title="Delete"
              >
                <TrashIcon className="size-3" />
              </button>
            </div>
          </FileTreeFile>
        );
      })}
    </>
  );
}

// ---------------------------------------------------------------------------
// WorkspacePanel
// ---------------------------------------------------------------------------

export function WorkspacePanel({ sessionId }: { sessionId: string | null }) {
  const workspaceTree = useAppStore((s) => s.workspaceTree);
  const workspaceRoot = useAppStore((s) => s.workspaceRoot);
  const workspaceTreeLoading = useAppStore((s) => s.workspaceTreeLoading);
  const workspaceTreeError = useAppStore((s) => s.workspaceTreeError);
  const fetchWorkspaceTree = useAppStore((s) => s.fetchWorkspaceTree);
  const fetchWorkspaceFile = useAppStore((s) => s.fetchWorkspaceFile);
  const clearWorkspace = useAppStore((s) => s.clearWorkspace);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const selectedPath = useAppStore((s) => s.selectedFilePath);
  const setSelectedPath = useAppStore((s) => s.setSelectedFilePath);

  // Fetch tree when session changes.
  useEffect(() => {
    if (sessionId) {
      void fetchWorkspaceTree(sessionId);
    } else {
      clearWorkspace();
    }
  }, [sessionId, fetchWorkspaceTree, clearWorkspace]);

  // Check if a path is a file (not a directory) by walking the tree.
  const isFilePath = useCallback(
    (path: string): boolean => {
      const find = (entries: FileEntry[]): boolean => {
        for (const e of entries) {
          if (e.path === path) return e.kind === "file";
          if (e.kind === "dir" && e.children && find(e.children)) return true;
        }
        return false;
      };
      return find(workspaceTree);
    },
    [workspaceTree],
  );

  const handleSelect = useCallback(
    (path: string) => {
      setSelectedPath(path);
      // Only fetch content for files, not directories.
      if (sessionId && isFilePath(path)) {
        void fetchWorkspaceFile(sessionId, path);
      }
    },
    [sessionId, fetchWorkspaceFile, isFilePath],
  );

  const handleRefresh = useCallback(() => {
    if (sessionId) {
      void fetchWorkspaceTree(sessionId);
    }
  }, [sessionId, fetchWorkspaceTree]);

  const handleUpload = useCallback(
    async (files: FileList) => {
      if (!sessionId || files.length === 0) return;
      setUploading(true);
      try {
        for (const file of Array.from(files)) {
          await workspaceApi.uploadFile(sessionId, file);
        }
        toast.success(
          files.length === 1
            ? `Uploaded ${files[0].name}`
            : `Uploaded ${files.length} files`,
        );
        void fetchWorkspaceTree(sessionId);
      } catch (e) {
        toast.error((e as Error).message);
      } finally {
        setUploading(false);
        if (fileInputRef.current) fileInputRef.current.value = "";
      }
    },
    [sessionId, fetchWorkspaceTree],
  );

  const handleDelete = useCallback(
    async (path: string) => {
      if (!sessionId) return;
      try {
        await workspaceApi.deleteFile(sessionId, path);
        toast.success(`Deleted ${path.split("/").pop()}`);
        void fetchWorkspaceTree(sessionId);
      } catch (e) {
        toast.error((e as Error).message);
      } finally {
        setDeleteTarget(null);
      }
    },
    [sessionId, fetchWorkspaceTree],
  );

  const rootName = workspaceRoot ?? "workspace";

  // Controlled expanded state — auto-expand parent folders when
  // selectedPath changes (e.g. clicking a file link in a tool call).
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(
    () => new Set(collectExpandedPaths(workspaceTree)),
  );

  // Re-initialize when workspace tree changes (new session).
  useEffect(() => {
    setExpandedPaths(new Set(collectExpandedPaths(workspaceTree)));
  }, [workspaceTree]);

  // Auto-expand ancestors of the selected file.
  useEffect(() => {
    if (!selectedPath) return;
    const parts = selectedPath.split("/");
    if (parts.length <= 1) return;
    setExpandedPaths((prev) => {
      const next = new Set(prev);
      let path = "";
      for (let i = 0; i < parts.length - 1; i++) {
        path = path ? `${path}/${parts[i]}` : parts[i];
        next.add(path);
      }
      return next;
    });
  }, [selectedPath]);

  return (
    <aside
      className={cn(
        "bg-card border-line border-muted flex flex-col overflow-hidden z-10",
        "w-150 min-w-150 transition-all duration-200",
      )}
    >
      {/* Hidden file input for uploads */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(e) => {
          if (e.target.files) void handleUpload(e.target.files);
        }}
      />

      {/* Header */}
      <div className="flex items-center px-3 py-3 border-b border-line min-h-14 gap-2">
        <FolderOpenIcon className="w-4 h-4 text-amber-500 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="font-medium text-sm text-foreground truncate">
            {rootName}
          </div>
          <div className="text-xs text-faint truncate">Workspace</div>
        </div>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
            >
              {uploading ? (
                <Loader2Icon className="w-5 h-5 animate-spin" />
              ) : (
                <UploadIcon className="w-5 h-5" />
              )}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Upload files</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              onClick={handleRefresh}
              disabled={workspaceTreeLoading}
            >
              <RefreshCwIcon
                className={cn("w-5 h-5", workspaceTreeLoading && "animate-spin")}
              />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Refresh</TooltipContent>
        </Tooltip>
      </div>

      {/* Tree content */}
      <ScrollArea className="flex-1 min-h-0">
        <div className="py-1 px-1">
          {workspaceTreeLoading && workspaceTree.length === 0 && (
            <div className="space-y-1 p-2">
              {Array.from({ length: 8 }).map((_, i) => (
                <Skeleton
                  key={i}
                  className="h-5 rounded"
                  style={{ width: `${SKELETON_WIDTHS[i % SKELETON_WIDTHS.length]}%` }}
                />
              ))}
            </div>
          )}

          {workspaceTreeError && (
            <div className="flex items-start gap-2 p-3 text-sm text-destructive">
              <AlertCircleIcon className="w-4 h-4 shrink-0 mt-0.5" />
              <span>{workspaceTreeError}</span>
            </div>
          )}

          {!workspaceTreeLoading && !workspaceTreeError && workspaceTree.length === 0 && (
            <div className="px-4 py-8 text-center text-sm text-faint">
              <p>No workspace files</p>
              <Button
                variant="outline"
                size="sm"
                className="mt-3 gap-1.5"
                onClick={() => fileInputRef.current?.click()}
              >
                <UploadIcon className="w-3.5 h-3.5" />
                Upload files
              </Button>
            </div>
          )}

          {workspaceTree.length > 0 && (
            <FileTree
              expanded={expandedPaths}
              onExpandedChange={setExpandedPaths}
              selectedPath={selectedPath}
              onSelect={handleSelect}
              className="border-0 rounded-none"
            >
              <RenderEntries
                entries={workspaceTree}
                sessionId={sessionId ?? ""}
                onFileSelect={handleSelect}
                onDelete={setDeleteTarget}
              />
            </FileTree>
          )}

        </div>
      </ScrollArea>

      {/* Inline file viewer — bottom split */}
      <FileViewer />

      {/* Delete confirmation */}
      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete file?"
        description={`This will permanently delete ${deleteTarget?.split("/").pop() ?? "this file"} from the workspace.`}
        confirmLabel="Delete"
        variant="destructive"
        onConfirm={() => deleteTarget ? handleDelete(deleteTarget) : Promise.resolve()}
        onCancel={() => setDeleteTarget(null)}
      />
    </aside>
  );
}

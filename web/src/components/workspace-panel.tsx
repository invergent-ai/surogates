// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useEffect, useState, useCallback, useRef } from "react";
import {
  FolderIcon,
  FolderOpenIcon,
  FileIcon,
  FileTextIcon,
  ChevronRightIcon,
  DownloadIcon,
  RefreshCwIcon,
  PanelRightCloseIcon,
  AlertCircleIcon,
  UploadIcon,
  TrashIcon,
  Loader2Icon,
} from "lucide-react";
import { toast } from "sonner";
import { useAppStore } from "@/stores/app-store";
import { cn } from "@/lib/utils";
import { formatFileSize } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { getAuthToken } from "@/features/auth";
import * as workspaceApi from "@/api/workspace";
import type { FileEntry } from "@/api/workspace";

// ---------------------------------------------------------------------------
// File icon helper
// ---------------------------------------------------------------------------

const TEXT_EXTENSIONS = new Set([
  ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".yaml", ".yml",
  ".toml", ".sql", ".sh", ".css", ".html", ".txt", ".csv", ".rst",
  ".cfg", ".ini", ".env", ".rs", ".go", ".java", ".rb", ".php",
  ".c", ".cpp", ".h", ".lock", ".gitignore", ".dockerignore", ".editorconfig",
]);

const SKELETON_WIDTHS = [75, 60, 90, 65, 80, 70, 85, 55];

function getFileExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot).toLowerCase() : "";
}

function isTextLike(name: string): boolean {
  return TEXT_EXTENSIONS.has(getFileExtension(name));
}

// ---------------------------------------------------------------------------
// FileTreeNode
// ---------------------------------------------------------------------------

function FileTreeNode({
  entry,
  depth,
  sessionId,
  onFileSelect,
  onDelete,
}: {
  entry: FileEntry;
  depth: number;
  sessionId: string;
  onFileSelect: (path: string) => void;
  onDelete: (path: string) => void;
}) {
  const [expanded, setExpanded] = useState(depth < 1);

  if (entry.kind === "dir") {
    const hasChildren = entry.children && entry.children.length > 0;
    return (
      <div>
        <button
          type="button"
          className={cn(
            "flex items-center gap-1 w-full text-left py-1 px-1 rounded-sm text-sm",
            "hover:bg-input transition-colors text-subtle hover:text-foreground",
          )}
          style={{ paddingLeft: `${depth * 12 + 4}px` }}
          onClick={() => setExpanded(!expanded)}
        >
          <ChevronRightIcon
            className={cn(
              "w-3 h-3 shrink-0 transition-transform duration-150",
              expanded && "rotate-90",
              !hasChildren && "invisible",
            )}
          />
          {expanded ? (
            <FolderOpenIcon className="w-4 h-4 shrink-0 text-amber-500" />
          ) : (
            <FolderIcon className="w-4 h-4 shrink-0 text-amber-500/70" />
          )}
          <span className="truncate">{entry.name}</span>
        </button>
        {expanded && hasChildren && (
          <div>
            {entry.children!.map((child) => (
              <FileTreeNode
                key={child.path}
                entry={child}
                depth={depth + 1}
                sessionId={sessionId}
                onFileSelect={onFileSelect}
                onDelete={onDelete}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  // File node.
  const textLike = isTextLike(entry.name);
  const FileIconComponent = textLike ? FileTextIcon : FileIcon;

  const downloadUrl = workspaceApi.getDownloadUrl(sessionId, entry.path);
  const token = getAuthToken();
  const downloadHref = token ? `${downloadUrl}&token=${encodeURIComponent(token)}` : downloadUrl;

  return (
    <div
      className={cn(
        "group/file flex items-center gap-0 w-full py-1 px-1 rounded-sm text-sm",
        "hover:bg-input transition-colors text-subtle hover:text-foreground",
      )}
      style={{ paddingLeft: `${depth * 12 + 20}px` }}
    >
      <button
        type="button"
        className={cn(
          "flex items-center gap-1 flex-1 min-w-0 text-left",
          textLike ? "cursor-pointer" : "cursor-default opacity-60",
        )}
        onClick={() => { if (textLike) onFileSelect(entry.path); }}
        disabled={!textLike}
      >
        <FileIconComponent className="w-4 h-4 shrink-0 text-muted-foreground" />
        <span className="truncate flex-1">{entry.name}</span>
        {entry.size != null && (
          <span className="text-xs text-faint shrink-0 ml-1">
            {formatFileSize(entry.size)}
          </span>
        )}
      </button>
      <div className="flex items-center gap-0 opacity-0 group-hover/file:opacity-100 transition-opacity shrink-0 ml-1">
        <Tooltip>
          <TooltipTrigger asChild>
            <a
              href={downloadHref}
              download
              className="p-0.5 rounded hover:bg-background/50 text-muted-foreground hover:text-foreground"
              onClick={(e) => e.stopPropagation()}
            >
              <DownloadIcon className="w-3 h-3" />
            </a>
          </TooltipTrigger>
          <TooltipContent side="bottom">Download</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              className="p-0.5 rounded hover:bg-destructive/10 text-muted-foreground hover:text-destructive"
              onClick={(e) => { e.stopPropagation(); onDelete(entry.path); }}
            >
              <TrashIcon className="w-3 h-3" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Delete</TooltipContent>
        </Tooltip>
      </div>
    </div>
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
  const workspaceTruncated = useAppStore((s) => s.workspaceTruncated);
  const fetchWorkspaceTree = useAppStore((s) => s.fetchWorkspaceTree);
  const fetchWorkspaceFile = useAppStore((s) => s.fetchWorkspaceFile);
  const setWorkspacePanelOpen = useAppStore((s) => s.setWorkspacePanelOpen);
  const workspacePanelOpen = useAppStore((s) => s.workspacePanelOpen);
  const clearWorkspace = useAppStore((s) => s.clearWorkspace);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  // Fetch tree when session changes.
  useEffect(() => {
    if (sessionId && workspacePanelOpen) {
      void fetchWorkspaceTree(sessionId);
    }
    if (!sessionId) {
      clearWorkspace();
    }
  }, [sessionId, workspacePanelOpen, fetchWorkspaceTree, clearWorkspace]);

  const handleFileSelect = useCallback(
    (path: string) => {
      if (sessionId) {
        void fetchWorkspaceFile(sessionId, path);
      }
    },
    [sessionId, fetchWorkspaceFile],
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

  if (!workspacePanelOpen) return null;

  const rootName = workspaceRoot ?? "workspace";

  return (
    <aside
      className={cn(
        "bg-card border-l border-line flex flex-col overflow-hidden z-10",
        "w-72 min-w-72 transition-all duration-200",
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
              size="icon-xs"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
            >
              {uploading ? (
                <Loader2Icon className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <UploadIcon className="w-3.5 h-3.5" />
              )}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Upload files</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon-xs"
              onClick={handleRefresh}
              disabled={workspaceTreeLoading}
            >
              <RefreshCwIcon
                className={cn("w-3.5 h-3.5", workspaceTreeLoading && "animate-spin")}
              />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Refresh</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon-xs"
              onClick={() => setWorkspacePanelOpen(false)}
            >
              <PanelRightCloseIcon className="w-3.5 h-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Close panel</TooltipContent>
        </Tooltip>
      </div>

      {/* Tree content */}
      <ScrollArea className="flex-1">
        <div className="py-1 px-1">
          {workspaceTreeLoading && workspaceTree.length === 0 && (
            <div className="space-y-1 p-2">
              {Array.from({ length: 8 }).map((_, i) => (
                <Skeleton key={i} className="h-5 rounded" style={{ width: `${SKELETON_WIDTHS[i % SKELETON_WIDTHS.length]}%` }} />
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

          {workspaceTree.map((entry) => (
            <FileTreeNode
              key={entry.path}
              entry={entry}
              depth={0}
              sessionId={sessionId ?? ""}
              onFileSelect={handleFileSelect}
              onDelete={setDeleteTarget}
            />
          ))}

          {workspaceTruncated && (
            <div className="px-3 py-2 text-xs text-faint text-center border-t border-line mt-1">
              File tree truncated (too many entries)
            </div>
          )}
        </div>
      </ScrollArea>

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

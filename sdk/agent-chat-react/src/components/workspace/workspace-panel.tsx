import { useCallback, useEffect, useRef, useState } from "react";
import type { MouseEvent, ReactNode } from "react";
import {
  AlertCircleIcon,
  DownloadIcon,
  FolderOpenIcon,
  Loader2Icon,
  RefreshCwIcon,
  TrashIcon,
  UploadIcon,
} from "lucide-react";
import {
  FileTree,
  FileTreeFile,
  FileTreeFolder,
} from "../ai-elements/file-tree";
import { Button } from "../ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";
import { ScrollArea } from "../ui/scroll-area";
import { Skeleton } from "../ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "../ui/tooltip";
import { cn } from "../../lib/utils";
import { formatFileSize } from "../../lib/format";
import type {
  AgentChatAdapter,
  AgentChatWorkspaceEntry,
  AgentChatWorkspaceFile,
} from "../../types";
import { FileViewer } from "./file-viewer";

const TEXT_EXTENSIONS = new Set([
  ".py",
  ".js",
  ".ts",
  ".tsx",
  ".jsx",
  ".json",
  ".md",
  ".yaml",
  ".yml",
  ".toml",
  ".sql",
  ".sh",
  ".css",
  ".html",
  ".txt",
  ".csv",
  ".rst",
  ".cfg",
  ".ini",
  ".env",
  ".rs",
  ".go",
  ".java",
  ".rb",
  ".php",
  ".c",
  ".cpp",
  ".h",
  ".lock",
  ".gitignore",
  ".dockerignore",
  ".editorconfig",
]);

const IMAGE_EXTENSIONS = new Set([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".svg",
  ".bmp",
  ".ico",
  ".avif",
  ".tiff",
  ".tif",
]);

const SKELETON_WIDTHS = [75, 60, 90, 65, 80, 70, 85, 55];
const DEFAULT_WIDTH = 500;
const MIN_WIDTH = 300;
const MAX_WIDTH = 900;

interface WorkspacePanelProps {
  adapter: AgentChatAdapter;
  sessionId: string | null;
  selectedPath: string | null;
  onSelectedPathChange: (path: string | null) => void;
}

function isViewable(name: string): boolean {
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot).toLowerCase() : "";
  return TEXT_EXTENSIONS.has(ext) || IMAGE_EXTENSIONS.has(ext);
}

function collectExpandedPaths(
  entries: AgentChatWorkspaceEntry[],
  depth = 0,
): string[] {
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

function findEntry(
  entries: AgentChatWorkspaceEntry[],
  path: string,
): AgentChatWorkspaceEntry | null {
  for (const entry of entries) {
    if (entry.path === path) return entry;
    if (entry.kind === "dir" && entry.children) {
      const found = findEntry(entry.children, path);
      if (found) return found;
    }
  }
  return null;
}

function RenderEntries({
  entries,
  onFileSelect,
  onDelete,
}: {
  entries: AgentChatWorkspaceEntry[];
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
                  onFileSelect={onFileSelect}
                  onDelete={onDelete}
                />
              )}
            </FileTreeFolder>
          );
        }

        const viewable = isViewable(entry.name);

        return (
          <FileTreeFile key={entry.path} name={entry.name} path={entry.path}>
            <span className="size-4 shrink-0" />
            <span className="min-w-0 flex-1 truncate">{entry.name}</span>
            {entry.size != null && (
              <span className="ml-1 shrink-0 text-xs text-muted-foreground/60">
                {formatFileSize(entry.size)}
              </span>
            )}
            <div className="ml-1 flex shrink-0 items-center gap-0 opacity-0 transition-opacity group-hover:opacity-100">
              {viewable && (
                <button
                  type="button"
                  className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
                  onClick={(event) => {
                    event.stopPropagation();
                    onFileSelect(entry.path);
                  }}
                  title="View"
                >
                  <DownloadIcon className="size-3" />
                </button>
              )}
              <button
                type="button"
                className="rounded p-0.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                onClick={(event) => {
                  event.stopPropagation();
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

export function WorkspacePanel({
  adapter,
  sessionId,
  selectedPath,
  onSelectedPathChange,
}: WorkspacePanelProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [entries, setEntries] = useState<AgentChatWorkspaceEntry[]>([]);
  const [rootName, setRootName] = useState("workspace");
  const [treeLoading, setTreeLoading] = useState(false);
  const [treeError, setTreeError] = useState<string | null>(null);
  const [file, setFile] = useState<AgentChatWorkspaceFile | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const isResizing = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(DEFAULT_WIDTH);

  const fetchTree = useCallback(async () => {
    if (!sessionId) {
      setEntries([]);
      setRootName("workspace");
      setTreeError(null);
      return;
    }
    setTreeLoading(true);
    setTreeError(null);
    try {
      const tree = await adapter.getWorkspaceTree({ sessionId });
      setEntries(tree.entries);
      setRootName(tree.root || "workspace");
      setExpandedPaths(new Set(collectExpandedPaths(tree.entries)));
    } catch (error) {
      setEntries([]);
      setRootName("workspace");
      setTreeError((error as Error).message);
    } finally {
      setTreeLoading(false);
    }
  }, [adapter, sessionId]);

  const fetchFile = useCallback(
    async (path: string) => {
      if (!sessionId) return;
      setFileLoading(true);
      setFileError(null);
      try {
        const nextFile = await adapter.getWorkspaceFile({ sessionId, path });
        setFile(nextFile);
      } catch (error) {
        setFile(null);
        setFileError((error as Error).message);
      } finally {
        setFileLoading(false);
      }
    },
    [adapter, sessionId],
  );

  useEffect(() => {
    void fetchTree();
  }, [fetchTree]);

  useEffect(() => {
    if (!sessionId) {
      setFile(null);
      setFileError(null);
      onSelectedPathChange(null);
    }
  }, [onSelectedPathChange, sessionId]);

  useEffect(() => {
    if (!selectedPath) return;
    const parts = selectedPath.split("/");
    if (parts.length <= 1) return;
    setExpandedPaths((prev) => {
      const next = new Set(prev);
      let path = "";
      for (let index = 0; index < parts.length - 1; index += 1) {
        path = path ? `${path}/${parts[index]}` : parts[index];
        next.add(path);
      }
      return next;
    });
  }, [selectedPath]);

  useEffect(() => {
    if (!selectedPath || !sessionId || entries.length === 0) return;
    const entry = findEntry(entries, selectedPath);
    if (entry?.kind === "file" && file?.path !== selectedPath) {
      void fetchFile(selectedPath);
    }
  }, [entries, fetchFile, file?.path, selectedPath, sessionId]);

  const handleSelect = useCallback(
    (path: string) => {
      onSelectedPathChange(path);
      const entry = findEntry(entries, path);
      if (entry?.kind === "file") {
        void fetchFile(path);
      }
    },
    [entries, fetchFile, onSelectedPathChange],
  );

  const handleUpload = useCallback(
    async (files: FileList) => {
      if (!sessionId || files.length === 0) return;
      setUploading(true);
      setNotice(null);
      try {
        for (const uploadedFile of Array.from(files)) {
          await adapter.uploadWorkspaceFile({
            sessionId,
            file: uploadedFile,
          });
        }
        setNotice(
          files.length === 1
            ? `Uploaded ${files[0]?.name ?? "file"}`
            : `Uploaded ${files.length} files`,
        );
        await fetchTree();
      } catch (error) {
        setNotice((error as Error).message);
      } finally {
        setUploading(false);
        if (fileInputRef.current) fileInputRef.current.value = "";
      }
    },
    [adapter, fetchTree, sessionId],
  );

  const handleDelete = useCallback(
    async (path: string) => {
      if (!sessionId) return;
      try {
        await adapter.deleteWorkspaceFile({ sessionId, path });
        if (selectedPath === path) {
          onSelectedPathChange(null);
          setFile(null);
          setFileError(null);
        }
        setNotice(`Deleted ${path.split("/").pop() ?? path}`);
        await fetchTree();
      } catch (error) {
        setNotice((error as Error).message);
      } finally {
        setDeleteTarget(null);
      }
    },
    [adapter, fetchTree, onSelectedPathChange, selectedPath, sessionId],
  );

  const onResizeStart = useCallback(
    (event: MouseEvent) => {
      event.preventDefault();
      isResizing.current = true;
      startX.current = event.clientX;
      startWidth.current = width;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [width],
  );

  useEffect(() => {
    const onMouseMove = (event: globalThis.MouseEvent) => {
      if (!isResizing.current) return;
      const delta = startX.current - event.clientX;
      setWidth(
        Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, startWidth.current + delta)),
      );
    };
    const onMouseUp = () => {
      if (!isResizing.current) return;
      isResizing.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, []);

  return (
    <aside
      className="relative z-10 flex min-h-0 flex-col overflow-hidden border-l border-muted-foreground/20 bg-card"
      style={{ width, minWidth: MIN_WIDTH, maxWidth: MAX_WIDTH }}
    >
      <div
        className="absolute inset-y-0 left-0 z-20 w-1.5 cursor-col-resize transition-colors hover:bg-primary/20 active:bg-primary/30"
        onMouseDown={onResizeStart}
      />
      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(event) => {
          if (event.target.files) void handleUpload(event.target.files);
        }}
      />

      <div className="flex min-h-14 items-center gap-2 border-b border-line px-3 py-3">
        <FolderOpenIcon className="size-4 shrink-0 text-amber-500" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-foreground">
            {rootName}
          </div>
          <div className="truncate text-xs text-faint">Workspace</div>
        </div>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => fileInputRef.current?.click()}
              disabled={!sessionId || uploading}
              aria-label="Upload files"
            >
              {uploading ? (
                <Loader2Icon className="size-4 animate-spin" />
              ) : (
                <UploadIcon className="size-4" />
              )}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Upload files</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => void fetchTree()}
              disabled={!sessionId || treeLoading}
              aria-label="Refresh workspace"
            >
              <RefreshCwIcon
                className={cn("size-4", treeLoading && "animate-spin")}
              />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Refresh</TooltipContent>
        </Tooltip>
      </div>

      {notice && (
        <div className="border-b border-line px-3 py-2 text-xs text-muted-foreground">
          {notice}
        </div>
      )}

      <ScrollArea className="min-h-0 flex-1">
        <div className="px-1 py-1">
          {treeLoading && entries.length === 0 && (
            <div className="space-y-1 p-2">
              {Array.from({ length: 8 }).map((_, index) => (
                <Skeleton
                  key={index}
                  className="h-5 rounded"
                  style={{
                    width: `${SKELETON_WIDTHS[index % SKELETON_WIDTHS.length]}%`,
                  }}
                />
              ))}
            </div>
          )}

          {treeError && (
            <div className="flex items-start gap-2 p-3 text-sm text-destructive">
              <AlertCircleIcon className="mt-0.5 size-4 shrink-0" />
              <span>{treeError}</span>
            </div>
          )}

          {!treeLoading && !treeError && entries.length === 0 && (
            <div className="px-4 py-8 text-center text-sm text-faint">
              <p>No workspace files</p>
              <Button
                variant="outline"
                size="sm"
                className="mt-3 gap-1.5"
                onClick={() => fileInputRef.current?.click()}
                disabled={!sessionId}
              >
                <UploadIcon className="size-3.5" />
                Upload files
              </Button>
            </div>
          )}

          {entries.length > 0 && (
            <FileTree
              expanded={expandedPaths}
              onExpandedChange={setExpandedPaths}
              selectedPath={selectedPath}
              onSelect={handleSelect}
              className="rounded-none border-0"
            >
              <RenderEntries
                entries={entries}
                onFileSelect={handleSelect}
                onDelete={setDeleteTarget}
              />
            </FileTree>
          )}
        </div>
      </ScrollArea>

      <FileViewer
        file={file}
        loading={fileLoading}
        error={fileError}
        onClose={() => {
          setFile(null);
          setFileError(null);
        }}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete file?"
        description={`This will permanently delete ${
          deleteTarget?.split("/").pop() ?? "this file"
        } from the workspace.`}
        confirmLabel="Delete"
        onConfirm={() =>
          deleteTarget ? handleDelete(deleteTarget) : Promise.resolve()
        }
        onCancel={() => setDeleteTarget(null)}
      />
    </aside>
  );
}

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  onConfirm: () => Promise<void> | void;
  onCancel: () => void;
}

function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const [loading, setLoading] = useState(false);

  const handleConfirm = async () => {
    setLoading(true);
    try {
      await onConfirm();
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen && !loading) onCancel();
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" disabled={loading} onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="destructive" disabled={loading} onClick={handleConfirm}>
            {loading && <Loader2Icon className="mr-1.5 size-3.5 animate-spin" />}
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}


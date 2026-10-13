import {
  AlertCircleIcon,
  DownloadIcon,
  FileTextIcon,
  ImageIcon,
  TrashIcon,
  XIcon,
} from "lucide-react";
import { formatFileSize, getLanguageHint } from "../../lib/format";
import { cn } from "../../lib/utils";
import { ScrollArea } from "../ui/scroll-area";
import { Skeleton } from "../ui/skeleton";
import type { AgentChatWorkspaceFile } from "../../types";

const SKELETON_WIDTHS = [70, 85, 55, 90, 60, 78, 45, 82, 65, 72];

interface FileViewerProps {
  file: AgentChatWorkspaceFile | null;
  loading: boolean;
  error: string | null;
  /** Same-origin URL the browser can navigate to for download. */
  downloadUrl: string | null;
  onDelete: (() => void) | null;
  onClose: () => void;
  /** Hide the built-in filename/download/close header. Use when the
   *  parent (e.g. a Dialog) already renders its own title bar. */
  hideHeader?: boolean;
  className?: string;
}

export function FileViewer({
  file,
  loading,
  error,
  downloadUrl,
  onDelete,
  onClose,
  hideHeader = false,
  className,
}: FileViewerProps) {
  const visible = loading || file !== null || error !== null;
  if (!visible) return null;

  const fileName = file?.path.split("/").pop() ?? "File";
  const lang = file ? getLanguageHint(file.path) : "";
  const isImage = file?.encoding === "base64" && (file.mime_type?.startsWith("image/") ?? false);
  const isBinary = file?.encoding === "base64" && !isImage;
  const HeaderIcon = isImage ? ImageIcon : FileTextIcon;

  return (
    <div className={cn("flex min-h-0 flex-1 flex-col border-t border-line", className)}>
      {!hideHeader && (
        <div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2">
          <HeaderIcon className="size-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0 flex-1">
            <div className="truncate font-medium">{fileName}</div>
            <div className="truncate text-sm text-muted-foreground">
              {file?.path ?? "Loading..."}
              {file && (
                <span className="ml-1.5">
                  {formatFileSize(file.size)}
                  {file.truncated && " (truncated)"}
                </span>
              )}
            </div>
          </div>
          {downloadUrl && (
            <a
              href={downloadUrl}
              download={fileName}
              className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              aria-label={`Download ${fileName}`}
              title="Download"
            >
              <DownloadIcon className="size-4" />
            </a>
          )}
          {onDelete && (
            <button
              type="button"
              onClick={onDelete}
              className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
              aria-label={`Delete ${fileName}`}
              title="Delete"
            >
              <TrashIcon className="size-4" />
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            aria-label="Close file"
          >
            <XIcon className="size-4" />
          </button>
        </div>
      )}

      <ScrollArea className="min-h-0 flex-1">
        {loading && (
          <div className="space-y-1.5 p-3">
            {Array.from({ length: 10 }).map((_, index) => (
              <Skeleton
                key={index}
                className="h-3.5 rounded"
                style={{
                  width: `${SKELETON_WIDTHS[index % SKELETON_WIDTHS.length]}%`,
                }}
              />
            ))}
          </div>
        )}

        {error && (
          <div className="flex items-start gap-2 p-3 text-xs text-destructive">
            <AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {file && isBinary && (
          <FileViewerError message="Preview not available. Download the file to open it." />
        )}

        {file && isImage && (
          <div className="flex items-center justify-center p-4">
            <img
              src={`data:${file.mime_type ?? "image/png"};base64,${file.content}`}
              alt={fileName}
              className="max-h-[60vh] max-w-full rounded object-contain"
            />
          </div>
        )}

        {file && !isImage && !isBinary && (
          <pre className="wrap-break-word whitespace-pre-wrap p-3 text-base leading-relaxed text-foreground">
            <code data-language={lang}>{file.content}</code>
          </pre>
        )}
      </ScrollArea>
    </div>
  );
}

function FileViewerError({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 p-3 text-xs text-destructive">
      <AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

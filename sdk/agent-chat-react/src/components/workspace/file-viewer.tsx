import {
  AlertCircleIcon,
  FileTextIcon,
  ImageIcon,
  XIcon,
} from "lucide-react";
import { formatFileSize, getLanguageHint } from "../../lib/format";
import { ScrollArea } from "../ui/scroll-area";
import { Skeleton } from "../ui/skeleton";
import type { AgentChatWorkspaceFile } from "../../types";

const SKELETON_WIDTHS = [70, 85, 55, 90, 60, 78, 45, 82, 65, 72];

interface FileViewerProps {
  file: AgentChatWorkspaceFile | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
}

export function FileViewer({
  file,
  loading,
  error,
  onClose,
}: FileViewerProps) {
  const visible = loading || file !== null || error !== null;
  if (!visible) return null;

  const fileName = file?.path.split("/").pop() ?? "File";
  const lang = file ? getLanguageHint(file.path) : "";
  const isImage = file?.encoding === "base64";
  const HeaderIcon = isImage ? ImageIcon : FileTextIcon;

  return (
    <div className="flex min-h-0 flex-1 flex-col border-t border-line">
      <div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2">
        <HeaderIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-medium">{fileName}</div>
          <div className="truncate text-[10px] text-muted-foreground">
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
          onClick={onClose}
          className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          aria-label="Close file"
        >
          <XIcon className="size-3.5" />
        </button>
      </div>

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

        {file && isImage && (
          <div className="flex items-center justify-center p-4">
            <img
              src={`data:${file.mime_type ?? "image/png"};base64,${file.content}`}
              alt={fileName}
              className="max-h-[60vh] max-w-full rounded object-contain"
            />
          </div>
        )}

        {file && !isImage && (
          <pre className="wrap-break-word whitespace-pre-wrap p-3 text-[11px] leading-relaxed text-foreground">
            <code data-language={lang}>{file.content}</code>
          </pre>
        )}
      </ScrollArea>
    </div>
  );
}


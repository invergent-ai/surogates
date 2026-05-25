import { useEffect, useRef, useState } from "react";
import {
  AlertCircleIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  DownloadIcon,
  FileTextIcon,
  ImageIcon,
  MinusIcon,
  PlusIcon,
  SearchIcon,
  TrashIcon,
  XIcon,
} from "lucide-react";
import "pdfjs-dist/web/pdf_viewer.css";
import { formatFileSize, getLanguageHint } from "../../lib/format";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { ScrollArea } from "../ui/scroll-area";
import { Skeleton } from "../ui/skeleton";
import type { AgentChatWorkspaceFile } from "../../types";
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
} from "pdfjs-dist";
import type {
  EventBus,
  PDFFindController,
  PDFViewer,
} from "pdfjs-dist/web/pdf_viewer.mjs";

const SKELETON_WIDTHS = [70, 85, 55, 90, 60, 78, 45, 82, 65, 72];
const PDF_WORKER_SRC = new URL(
  "pdfjs-dist/legacy/build/pdf.worker.mjs",
  import.meta.url,
).toString();

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
}

export function FileViewer({
  file,
  loading,
  error,
  downloadUrl,
  onDelete,
  onClose,
  hideHeader = false,
}: FileViewerProps) {
  const visible = loading || file !== null || error !== null;
  if (!visible) return null;

  const fileName = file?.path.split("/").pop() ?? "File";
  const lang = file ? getLanguageHint(file.path) : "";
  const isPdf =
    file?.mime_type === "application/pdf" ||
    file?.path.toLowerCase().endsWith(".pdf") ||
    false;
  const isImage = file?.encoding === "base64" && !isPdf;
  const HeaderIcon = isImage ? ImageIcon : FileTextIcon;

  return (
    <div className="flex min-h-0 flex-1 flex-col border-t border-line">
      {!hideHeader && (
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
          {downloadUrl && (
            <a
              href={downloadUrl}
              download={fileName}
              className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              aria-label={`Download ${fileName}`}
              title="Download"
            >
              <DownloadIcon className="size-3.5" />
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
              <TrashIcon className="size-3.5" />
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            aria-label="Close file"
          >
            <XIcon className="size-3.5" />
          </button>
        </div>
      )}

      {file && isPdf && file.encoding === "base64" ? (
        // PDF has its own scrollable container — bypass ScrollArea so
        // it can grow with the parent's flex height (Radix Viewport's
        // internal table wrapper sizes to content and breaks h-full).
        <PdfPreview file={file} fileName={fileName} />
      ) : (
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

          {file && isPdf && file.encoding !== "base64" && (
            <FileViewerError message="PDF preview requires base64 file content." />
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

          {file && !isImage && !isPdf && (
            <pre className="wrap-break-word whitespace-pre-wrap p-3 text-[11px] leading-relaxed text-foreground">
              <code data-language={lang}>{file.content}</code>
            </pre>
          )}
        </ScrollArea>
      )}
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

function PdfPreview({
  file,
  fileName,
}: {
  file: AgentChatWorkspaceFile;
  fileName: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<HTMLDivElement>(null);
  const pdfViewerRef = useRef<PDFViewer | null>(null);
  const eventBusRef = useRef<EventBus | null>(null);
  const findControllerRef = useRef<PDFFindController | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [pagesCount, setPagesCount] = useState(0);
  const [scale, setScale] = useState(1);
  const [query, setQuery] = useState("");

  useEffect(() => {
    let cancelled = false;
    let loadingTask: PDFDocumentLoadingTask | null = null;
    let pdfDocument: PDFDocumentProxy | null = null;

    const renderPdf = async () => {
      setError(null);
      const container = containerRef.current;
      const viewerElement = viewerRef.current;
      if (!container || !viewerElement) return;

      const pdfBytes = decodeBase64(file.content);
      const pdfjs = await import("pdfjs-dist/legacy/build/pdf.mjs");
      pdfjs.GlobalWorkerOptions.workerSrc = PDF_WORKER_SRC;
      (
        globalThis as typeof globalThis & {
          pdfjsLib?: typeof pdfjs;
        }
      ).pdfjsLib = pdfjs;
      const pdfViewerModule = await import("pdfjs-dist/web/pdf_viewer.mjs");
      loadingTask = pdfjs.getDocument({ data: pdfBytes });
      const pdf = await loadingTask.promise;
      pdfDocument = pdf;
      if (cancelled) return;

      const eventBus = new pdfViewerModule.EventBus();
      const linkService = new pdfViewerModule.PDFLinkService({ eventBus });
      const findController = new pdfViewerModule.PDFFindController({
        eventBus,
        linkService,
      });
      const pdfViewer = new pdfViewerModule.PDFViewer({
        container,
        viewer: viewerElement,
        eventBus,
        linkService,
        findController,
        removePageBorders: true,
      });
      linkService.setViewer(pdfViewer);
      linkService.setDocument(pdf);
      findController.setDocument(pdf);

      const onPagesInit = () => {
        pdfViewer.currentScaleValue = "page-width";
        setScale(pdfViewer.currentScale || 1);
        setPagesCount(pdfViewer.pagesCount);
        setPageNumber(pdfViewer.currentPageNumber);
      };
      const onPageChanging = (event: { pageNumber?: number }) => {
        if (typeof event.pageNumber === "number") {
          setPageNumber(event.pageNumber);
        }
      };
      const onScaleChanging = (event: { scale?: number }) => {
        if (typeof event.scale === "number") {
          setScale(event.scale);
        }
      };

      eventBus.on("pagesinit", onPagesInit);
      eventBus.on("pagechanging", onPageChanging);
      eventBus.on("scalechanging", onScaleChanging);

      pdfViewerRef.current = pdfViewer;
      eventBusRef.current = eventBus;
      findControllerRef.current = findController;
      setPagesCount(pdf.numPages);
      pdfViewer.setDocument(pdf);
    };

    void renderPdf().catch((nextError) => {
      if (cancelled) return;
      if (
        nextError instanceof Error &&
        nextError.name === "RenderingCancelledException"
      ) {
        return;
      }
      setError(formatPdfPreviewError(nextError));
    });

    return () => {
      cancelled = true;
      loadingTask?.destroy();
      pdfDocument?.destroy();
      pdfViewerRef.current?.cleanup();
      pdfViewerRef.current = null;
      eventBusRef.current = null;
      findControllerRef.current = null;
    };
  }, [file.content]);

  const setViewerPage = (nextPage: number) => {
    const viewer = pdfViewerRef.current;
    if (!viewer) return;
    const clampedPage = Math.min(
      Math.max(1, nextPage),
      Math.max(1, viewer.pagesCount),
    );
    viewer.currentPageNumber = clampedPage;
    setPageNumber(clampedPage);
  };

  const setViewerScale = (nextScale: number) => {
    const viewer = pdfViewerRef.current;
    if (!viewer) return;
    const clampedScale = Math.min(3, Math.max(0.5, nextScale));
    viewer.currentScale = clampedScale;
    setScale(clampedScale);
  };

  const runFind = (findPrevious = false) => {
    const eventBus = eventBusRef.current;
    if (!eventBus || !query.trim()) return;
    eventBus.dispatch("find", {
      source: eventBus,
      type: "again",
      query,
      phraseSearch: true,
      caseSensitive: false,
      entireWord: false,
      highlightAll: true,
      findPrevious,
      matchDiacritics: true,
    });
  };

  return (
    <div className="flex min-h-full flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-line px-2 py-1.5">
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          onClick={() => setViewerPage(pageNumber - 1)}
          disabled={pageNumber <= 1}
          aria-label="Previous PDF page"
          title="Previous page"
        >
          <ChevronLeftIcon className="size-3.5" />
        </Button>
        <Input
          type="number"
          min={1}
          max={pagesCount || 1}
          value={pageNumber}
          onChange={(event) => setViewerPage(Number(event.target.value))}
          aria-label="PDF page number"
          className="h-7 w-12 border-input px-1 text-center text-xs"
        />
        <span className="text-xs text-muted-foreground">
          / {pagesCount || "-"}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          onClick={() => setViewerPage(pageNumber + 1)}
          disabled={pagesCount > 0 && pageNumber >= pagesCount}
          aria-label="Next PDF page"
          title="Next page"
        >
          <ChevronRightIcon className="size-3.5" />
        </Button>

        <div className="mx-1 h-5 w-px bg-line" />

        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          onClick={() => setViewerScale(scale - 0.1)}
          disabled={scale <= 0.5}
          aria-label="Zoom out PDF"
          title="Zoom out"
        >
          <MinusIcon className="size-3.5" />
        </Button>
        <button
          type="button"
          className="h-7 min-w-12 px-1 text-xs text-muted-foreground hover:text-foreground"
          onClick={() => {
            const viewer = pdfViewerRef.current;
            if (!viewer) return;
            viewer.currentScaleValue = "page-width";
            setScale(viewer.currentScale || 1);
          }}
          aria-label="Fit PDF to width"
          title="Fit width"
        >
          {Math.round(scale * 100)}%
        </button>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          onClick={() => setViewerScale(scale + 0.1)}
          disabled={scale >= 3}
          aria-label="Zoom in PDF"
          title="Zoom in"
        >
          <PlusIcon className="size-3.5" />
        </Button>

        <div className="mx-1 h-5 w-px bg-line" />

        <SearchIcon className="size-3.5 text-muted-foreground" />
        <Input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") runFind(event.shiftKey);
          }}
          placeholder="Find"
          aria-label="Find in PDF"
          className="h-7 w-28 border-input px-1 text-xs"
        />
        <Button
          type="button"
          variant="ghost"
          size="xs"
          onClick={() => runFind(false)}
          disabled={!query.trim()}
        >
          Find
        </Button>
      </div>
      {error && (
        <div className="flex w-full items-start gap-2 p-3 text-xs text-destructive">
          <AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {!error && (
        <div className="relative min-h-105 flex-1 bg-muted/20">
          <div
            ref={containerRef}
            aria-label={`PDF viewer for ${fileName}`}
            className="absolute inset-0 overflow-auto"
          >
            <div ref={viewerRef} className="pdfViewer" />
          </div>
        </div>
      )}
    </div>
  );
}

function decodeBase64(content: string): Uint8Array {
  const binary = globalThis.atob(content);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function formatPdfPreviewError(error: unknown): string {
  if (
    error instanceof Error &&
    (error.name === "InvalidCharacterError" || error.message === "Invalid character")
  ) {
    return "PDF preview data is not valid base64.";
  }
  return error instanceof Error ? error.message : "Failed to render PDF preview.";
}

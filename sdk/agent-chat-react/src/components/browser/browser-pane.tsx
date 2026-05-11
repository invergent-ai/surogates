import { useCallback, useEffect, useMemo, useState } from "react";
import { Maximize2Icon, PlayIcon, ZapIcon } from "lucide-react";
import { BrowserControlBar } from "./browser-control-bar";
import { BrowserLiveView } from "./browser-live-view";
import { BrowserStatusDot } from "./browser-status-dot";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "../ui/tooltip";
import type {
  AgentChatAdapter,
  AgentChatBrowserPreviewSnapshot,
  AgentChatBrowserState,
} from "../../types";

type BrowserPaneAdapter = Pick<
  AgentChatAdapter,
  | "browserLiveViewUrl"
  | "getBrowserPreviewSnapshot"
  | "acquireBrowserControl"
  | "releaseBrowserControl"
>;

interface BrowserPaneProps {
  sessionId: string;
  state: AgentChatBrowserState;
  adapter: BrowserPaneAdapter;
}

export function BrowserPane({ sessionId, state, adapter }: BrowserPaneProps) {
  const [fullscreenOpen, setFullscreenOpen] = useState(false);
  const [inlinePreviewOpen, setInlinePreviewOpen] = useState(false);
  const [previewSnapshot, setPreviewSnapshot] =
    useState<AgentChatBrowserPreviewSnapshot | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const hasLiveViewAdapter =
    typeof adapter.browserLiveViewUrl === "function";
  const hasPreviewAdapter =
    typeof adapter.getBrowserPreviewSnapshot === "function";
  const hasControlAdapter =
    typeof adapter.acquireBrowserControl === "function" &&
    typeof adapter.releaseBrowserControl === "function";
  const liveViewUrl = useMemo(() => {
    if (!hasLiveViewAdapter) return "";
    return adapter.browserLiveViewUrl(sessionId);
  }, [adapter, hasLiveViewAdapter, sessionId]);
  const hasLiveView = state.status !== "provisioning" && state.status !== "closed";
  const hasUserControl = state.status === "user-control";
  const canUseLiveView = hasUserControl && Boolean(liveViewUrl);
  const canOpenPreview = hasLiveView && (hasPreviewAdapter || canUseLiveView);
  const canOpenFullscreen = canOpenPreview;

  const refreshPreview = useCallback(
    async (signal?: AbortSignal) => {
      if (!hasPreviewAdapter || canUseLiveView) return;
      setPreviewLoading(true);
      setPreviewError(null);
      try {
        const snapshot = await adapter.getBrowserPreviewSnapshot?.(sessionId);
        if (signal?.aborted) return;
        setPreviewSnapshot(snapshot ?? null);
        if (!snapshot) {
          setPreviewError("Browser preview is unavailable.");
        }
      } catch (error) {
        if (signal?.aborted) return;
        setPreviewSnapshot(null);
        setPreviewError(
          error instanceof Error ? error.message : "Browser preview failed.",
        );
      } finally {
        if (!signal?.aborted) setPreviewLoading(false);
      }
    },
    [adapter, canUseLiveView, hasPreviewAdapter, sessionId],
  );

  useEffect(() => {
    setFullscreenOpen(false);
    setInlinePreviewOpen(false);
    setPreviewSnapshot(null);
    setPreviewLoading(false);
    setPreviewError(null);
  }, [sessionId]);

  useEffect(() => {
    if (!hasLiveView || canUseLiveView) {
      setPreviewLoading(false);
      setPreviewError(null);
      return;
    }
    if (!inlinePreviewOpen && !fullscreenOpen) return;

    const abort = new AbortController();
    void refreshPreview(abort.signal);
    const timer = window.setInterval(() => {
      void refreshPreview(abort.signal);
    }, 5000);
    return () => {
      abort.abort();
      window.clearInterval(timer);
    };
  }, [
    canUseLiveView,
    fullscreenOpen,
    hasLiveView,
    inlinePreviewOpen,
    refreshPreview,
  ]);

  return (
    <>
      <div
        data-testid="browser-pane"
        className="flex h-full min-h-0 flex-col bg-background"
      >
        <header className="flex min-h-10 items-center gap-2 border-b border-line bg-card px-3 text-xs text-foreground">
          <ZapIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
          <span className="font-medium">Browser</span>
          <BrowserStatusDot status={state.status} />
          {state.controlOwner && (
            <span className="min-w-0 truncate text-amber-500">
              {state.controlOwner} has control
            </span>
          )}
          {canOpenFullscreen && (
            <div className="ml-auto flex items-center">
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      aria-label="Maximize browser"
                      className="inline-flex size-7 items-center justify-center text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      onClick={() => setFullscreenOpen(true)}
                    >
                      <Maximize2Icon className="size-3.5" aria-hidden="true" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="bottom">Full screen</TooltipContent>
                </Tooltip>
              </TooltipProvider>
            </div>
          )}
        </header>
        <div className="min-h-0 flex-1 bg-black">
          {state.status === "provisioning" ? (
            <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
              Starting browser...
            </div>
          ) : state.status === "closed" ? (
            <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
              Browser closed.
            </div>
          ) : canUseLiveView && inlinePreviewOpen ? (
            <BrowserLiveView src={liveViewUrl} />
          ) : inlinePreviewOpen && previewSnapshot ? (
            <BrowserPreviewImage
              src={previewSnapshot.src}
              testId="browser-preview-image"
            />
          ) : inlinePreviewOpen && previewLoading ? (
            <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
              Loading browser preview...
            </div>
          ) : inlinePreviewOpen && previewError ? (
            <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
              {previewError}
            </div>
          ) : canOpenPreview ? (
            <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
              <button
                type="button"
                aria-label="Open browser preview"
                className="inline-flex items-center gap-2 border border-line bg-card px-3 py-2 text-xs font-medium text-foreground transition-colors hover:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onClick={() => setInlinePreviewOpen(true)}
              >
                <PlayIcon className="size-3.5" aria-hidden="true" />
                <span>Open preview</span>
              </button>
            </div>
          ) : (
            <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
              Browser live view is unavailable.
            </div>
          )}
        </div>
        {hasLiveView && hasControlAdapter && (
          <BrowserControlBar
            sessionId={sessionId}
            hasControl={state.status === "user-control"}
            adapter={adapter}
          />
        )}
      </div>
      <Dialog open={fullscreenOpen} onOpenChange={setFullscreenOpen}>
        <DialogContent
          aria-describedby={undefined}
          className="flex h-screen w-screen max-w-none flex-col gap-0 overflow-hidden rounded-none border-0 bg-background p-0 shadow-none ring-0 sm:max-w-none"
        >
          <DialogHeader className="h-10 shrink-0 flex-row items-center gap-2 border-b border-line bg-card px-4 py-0">
            <ZapIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
            <DialogTitle className="text-xs normal-case tracking-normal">
              Browser
            </DialogTitle>
            <BrowserStatusDot status={state.status} />
            {state.controlOwner && (
              <span className="min-w-0 truncate text-xs text-amber-500">
                {state.controlOwner} has control
              </span>
            )}
          </DialogHeader>
          <div className="min-h-0 flex-1 bg-black">
            {canUseLiveView ? (
              <BrowserLiveView
                src={liveViewUrl}
                testId="browser-fullscreen-iframe"
              />
            ) : previewSnapshot ? (
              <BrowserPreviewImage
                src={previewSnapshot.src}
                testId="browser-fullscreen-preview-image"
              />
            ) : previewLoading ? (
              <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
                Loading browser preview...
              </div>
            ) : previewError ? (
              <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
                {previewError}
              </div>
            ) : (
              <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
                Browser preview is unavailable.
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function BrowserPreviewImage({
  src,
  testId,
}: {
  src: string;
  testId: string;
}) {
  return (
    <img
      data-testid={testId}
      src={src}
      alt="Browser preview"
      className="h-full w-full bg-black object-contain"
      draggable={false}
    />
  );
}

import { useCallback, useEffect, useMemo, useState } from "react";
import { BrowserShell } from "./browser-shell";
import { useBrowserControl } from "./use-browser-control";
import { ConfirmDialog } from "../ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";
import type {
  AgentChatAdapter,
  AgentChatBrowserState,
} from "../../types";

type BrowserPaneAdapter = Pick<
  AgentChatAdapter,
  | "browserShellUrl"
  | "acquireBrowserControl"
  | "releaseBrowserControl"
  | "closeBrowserSession"
>;

interface BrowserPaneProps {
  sessionId: string;
  state: AgentChatBrowserState;
  adapter: BrowserPaneAdapter;
  /**
   * Optional close handler. When supplied, a "Close" button is
   * offered in the shell's overflow menu. The
   * parent decides what closing means — typically hiding the pane.
   * If the user holds browser control at close time, the bar releases
   * it before invoking onClose so the agent can reclaim immediately.
   */
  onClose?: () => void;
}

export function BrowserPane({
  sessionId,
  state,
  adapter,
  onClose,
}: BrowserPaneProps) {
  const [fullscreenOpen, setFullscreenOpen] = useState(false);
  const [openFullscreenOnControl, setOpenFullscreenOnControl] = useState(false);
  const [localControlActive, setLocalControlActive] = useState(false);
  const hasShellAdapter = typeof adapter.browserShellUrl === "function";
  const hasControlAdapter =
    typeof adapter.acquireBrowserControl === "function" &&
    typeof adapter.releaseBrowserControl === "function";
  const shellUrl = useMemo(() => {
    if (!hasShellAdapter) return "";
    return adapter.browserShellUrl(sessionId);
  }, [adapter, hasShellAdapter, sessionId]);
  const hasLiveView = state.status !== "provisioning" && state.status !== "closed";
  // The shell streams whether or not this viewer holds the lease, so there is
  // no longer a no-control state to fall back to a still preview for.
  const canUseShell = hasLiveView && Boolean(shellUrl);

  useEffect(() => {
    setFullscreenOpen(false);
    setOpenFullscreenOnControl(false);
    setLocalControlActive(false);
  }, [sessionId]);

  useEffect(() => {
    if (!openFullscreenOnControl) return;
    if (!canUseShell) return;
    setFullscreenOpen(true);
    setOpenFullscreenOnControl(false);
  }, [canUseShell, openFullscreenOnControl]);

  // Closing the fullscreen dialog must NOT release browser control.
  // Control is a separate, user-driven concern (toggled via
  // the shell's take-control toggle). The shell streams without the
  // lease, so releasing here would cost the user their control for no
  // reason and hand the page back to the agent mid-edit.
  const handleFullscreenOpenChange = useCallback(
    (open: boolean) => {
      setFullscreenOpen(open);
    },
    [],
  );

  // Heartbeat: while the user holds control AND the live view is
  // mounted, refresh the lease at ~25s so the harness's 60s control
  // TTL never lapses under us. acquireBrowserControl for the same user
  // returns `refreshed` and resets the TTL — no extra API surface
  // needed. Gated on HOLDING control rather than on the view being
  // mounted: the shell streams without the lease, so the two are no longer
  // the same condition and keying on the view would refresh a lease this
  // viewer does not have.
  useEffect(() => {
    if (!localControlActive || !hasControlAdapter) return;
    const handle = window.setInterval(() => {
      void adapter.acquireBrowserControl(sessionId).catch((error) => {
        // Treat refresh failures as terminal — the lease is gone and
        // the iframe will close itself on the next backend check.
        console.error("Failed to refresh browser control", error);
        setLocalControlActive(false);
      });
    }, 25_000);
    return () => window.clearInterval(handle);
  }, [adapter, localControlActive, hasControlAdapter, sessionId]);

  const control = useBrowserControl({
    sessionId,
    hasControl: localControlActive,
    adapter,
    onControlAcquired: () => {
      setLocalControlActive(true);
      setOpenFullscreenOnControl(true);
    },
    onControlReleased: () => setLocalControlActive(false),
    onClose,
  });

  const shell = (testId?: string) => (
    <BrowserShell
      src={shellUrl}
      testId={testId}
      hasControl={localControlActive}
      onToggleControl={
        hasControlAdapter ? () => void control.toggleControl() : undefined
      }
      onClose={onClose ? () => control.setConfirmOpen(true) : undefined}
      onMaximize={
        fullscreenOpen ? undefined : () => setFullscreenOpen(true)
      }
      onDisconnect={() => setLocalControlActive(false)}
    />
  );

  const placeholder = (message: string) => (
    <div className="flex h-full items-center justify-center bg-background text-sm text-muted-foreground">
      {message}
    </div>
  );

  return (
    <>
      <div
        data-testid="browser-pane"
        className="flex h-full min-h-0 flex-col bg-background"
      >
        {/* No header row: the shell's own toolbar carries the URL, the
            controls and the overflow menu. Stacking a pane header above it
            put four bars over the page in a 660px pane. */}
        <div className="min-h-0 flex-1">
          {state.status === "provisioning"
            ? placeholder("Starting browser...")
            : state.status === "closed"
              ? placeholder("Browser closed.")
              : canUseShell
                ? shell()
                : placeholder("Browser view is unavailable.")}
        </div>
        {control.error && (
          <div className="border-t border-line bg-card px-3 py-1.5 text-[11px] text-destructive">
            {control.error}
          </div>
        )}
      </div>
      <Dialog open={fullscreenOpen} onOpenChange={handleFullscreenOpenChange}>
        <DialogContent
          aria-describedby={undefined}
          className="flex h-dvh w-screen max-w-none flex-col gap-0 overflow-hidden rounded-none border-0 bg-background p-0 shadow-none ring-0 sm:max-w-none"
        >
          <DialogHeader className="sr-only">
            <DialogTitle>Browser</DialogTitle>
          </DialogHeader>
          <div className="min-h-0 flex-1">
            {canUseShell
              ? shell("browser-fullscreen-shell")
              : placeholder("Browser view is unavailable.")}
          </div>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={control.confirmOpen}
        title="Close browser session?"
        description={
          adapter.closeBrowserSession
            ? "This permanently shuts down the browser sandbox for this session. The agent will lose access to the page until it re-opens a browser. This cannot be undone."
            : "This hides the browser panel. The sandbox stays running so the agent can keep using it."
        }
        confirmLabel="Close browser"
        variant="destructive"
        onConfirm={control.handleConfirmClose}
        onCancel={() => control.setConfirmOpen(false)}
      />
    </>
  );
}

import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { BrowserLiveView } from "@invergent/agent-chat-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { captureProfile, createSetupSession } from "@/api/browser-profiles";
import { surogatesWebChatAdapter } from "@/features/chat/surogates-web-chat-adapter";

interface Props {
  profileId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}

export function BrowserProfileSetupDialog({
  profileId,
  open,
  onOpenChange,
  onSaved,
}: Props) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [remaining, setRemaining] = useState(0);
  const [saving, setSaving] = useState(false);
  const [phase, setPhase] = useState<"starting" | "ready">("starting");
  const startedRef = useRef(false);
  // ``onOpenChange`` is a fresh arrow each render; ref it so effects don't
  // re-run (and re-create the session) on the parent's re-renders.
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;

  // Start the setup session exactly once per open. The guard is reset on close
  // (not in effect cleanup) so a later reopen starts fresh — resetting in
  // cleanup would let React StrictMode's mount→unmount→mount create two setup
  // sessions, i.e. two billed browsers.
  useEffect(() => {
    if (!open) {
      startedRef.current = false;
      return;
    }
    if (startedRef.current) return;
    startedRef.current = true;
    createSetupSession(profileId)
      .then((res) => {
        setSessionId(res.sessionId);
        setExpiresAt(new Date(res.expiresAt).getTime());
      })
      .catch(() => {
        toast.error("Couldn't start the setup browser.");
        onOpenChangeRef.current(false);
      });
  }, [open, profileId]);

  // Provisioning is worker-driven (async), so poll until the browser is up,
  // then take the control lease *before* flipping to "ready" — only then do we
  // mount the live view. The harness 403s the live-view WebSocket unless the
  // caller already holds control, so mounting it first would drop the RFB
  // connection on every render and loop forever on "Connecting…".
  useEffect(() => {
    if (!sessionId || phase === "ready") return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      if (cancelled) return;
      try {
        const state = await surogatesWebChatAdapter.getBrowserState(sessionId);
        if (
          !cancelled &&
          state &&
          (state.status === "live" || state.status === "user-control")
        ) {
          await surogatesWebChatAdapter.acquireBrowserControl(sessionId);
          if (!cancelled) {
            setPhase("ready");
            return;
          }
        }
      } catch {
        /* keep polling */
      }
      if (!cancelled) timer = window.setTimeout(poll, 1500);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [sessionId, phase]);

  // The readiness gate already took the control lease; renew it here every 25s
  // (the lease is 60s) so input doesn't go read-only mid-login — this dialog
  // renders BrowserLiveView directly, without BrowserPane's control bar.
  useEffect(() => {
    if (!sessionId || phase !== "ready") return;
    const beat = () => {
      void surogatesWebChatAdapter
        .acquireBrowserControl(sessionId)
        .catch(() => {});
    };
    beat();
    const h = window.setInterval(beat, 25_000);
    return () => window.clearInterval(h);
  }, [sessionId, phase]);

  // Countdown to the server-side TTL, closing only when it actually elapses.
  // The expiry decision uses the freshly-computed ``r`` — not the ``remaining``
  // state — so it can't fire on the first render (where ``remaining`` is still
  // its initial 0 the instant ``expiresAt`` is set).
  useEffect(() => {
    if (!expiresAt) return;
    const tick = () => {
      const r = Math.max(0, Math.round((expiresAt - Date.now()) / 1000));
      setRemaining(r);
      if (r <= 0) {
        toast.info("Setup session expired.");
        onOpenChangeRef.current(false);
      }
    };
    tick();
    const h = window.setInterval(tick, 1000);
    return () => window.clearInterval(h);
  }, [expiresAt]);

  const liveViewUrl = useMemo(
    () =>
      sessionId ? surogatesWebChatAdapter.browserLiveViewUrl(sessionId) : "",
    [sessionId],
  );

  async function handleSave() {
    if (!sessionId) return;
    setSaving(true);
    try {
      await captureProfile(profileId, sessionId);
      toast.success("Authentication saved.");
      onSaved();
    } catch {
      toast.error("Couldn't save authentication.");
    } finally {
      setSaving(false);
    }
  }

  const mmss = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-dvh w-screen max-w-none sm:max-w-none flex-col gap-0 rounded-none border-0 bg-background p-0">
        <DialogHeader className="h-12 shrink-0 flex-row items-center justify-between border-b border-line px-4">
          <DialogTitle className="text-sm">
            Set up browser authentication
          </DialogTitle>
          <div className="flex items-center gap-3">
            {expiresAt && (
              <span className="text-xs tabular-nums text-muted-foreground">
                {mmss}
              </span>
            )}
            <Button
              size="sm"
              onClick={handleSave}
              disabled={phase !== "ready" || saving}
            >
              {saving ? "Saving…" : "Save authentication and close"}
            </Button>
          </div>
        </DialogHeader>
        <div className="min-h-0 flex-1 bg-black">
          {phase === "ready" && liveViewUrl ? (
            <BrowserLiveView
              src={liveViewUrl}
              testId="browser-profile-setup-rfb"
              onDisconnect={() => setPhase("starting")}
            />
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              Starting browser…
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

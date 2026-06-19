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
  const startedRef = useRef(false);

  useEffect(() => {
    if (!open || startedRef.current) return;
    startedRef.current = true;
    createSetupSession(profileId)
      .then((res) => {
        setSessionId(res.sessionId);
        setExpiresAt(new Date(res.expiresAt).getTime());
      })
      .catch(() => {
        toast.error("Couldn't start the setup browser.");
        onOpenChange(false);
      });
    return () => {
      startedRef.current = false;
    };
  }, [open, profileId, onOpenChange]);

  useEffect(() => {
    if (!expiresAt) return;
    const tick = () =>
      setRemaining(Math.max(0, Math.round((expiresAt - Date.now()) / 1000)));
    tick();
    const h = window.setInterval(tick, 1000);
    return () => window.clearInterval(h);
  }, [expiresAt]);

  useEffect(() => {
    if (expiresAt && remaining === 0) {
      toast.info("Setup session expired.");
      onOpenChange(false);
    }
  }, [remaining, expiresAt, onOpenChange]);

  const liveViewUrl = useMemo(
    () => (sessionId ? surogatesWebChatAdapter.browserLiveViewUrl(sessionId) : ""),
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
      <DialogContent className="flex h-dvh w-screen max-w-none flex-col gap-0 rounded-none border-0 bg-background p-0">
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
            <Button size="sm" onClick={handleSave} disabled={!sessionId || saving}>
              {saving ? "Saving…" : "Save authentication and close"}
            </Button>
          </div>
        </DialogHeader>
        <div className="min-h-0 flex-1 bg-black">
          {liveViewUrl ? (
            <BrowserLiveView
              src={liveViewUrl}
              testId="browser-profile-setup-rfb"
              onDisconnect={() => onOpenChange(false)}
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

import { useEffect, useRef, useState } from "react";
import type { AgentChatAdapter } from "../../types";

/**
 * Poll the session's browser preview for the card's thumbnail.
 *
 * A thumbnail and nothing more. Whether the card shows at all is decided by
 * session state (`browser.provisioned` / `getBrowserState`), the way the old
 * live view decided — the registry self-heals server-side now, so that state
 * can be trusted. An earlier version of this hook also voted on liveness, and
 * every gate built on that vote misfired: a screenshot fails on a page
 * mid-navigation, which is precisely when the agent has just used the
 * browser, so "no frame" never meant "no browser".
 *
 * The card sits above the composer whether or not the pane is open, so it
 * cannot read the shell's live frames — those exist only while a viewer holds
 * the pane connected. The preview endpoint answers with the pane closed, at
 * the cost of a screenshot per refresh. That cost is why the poll is slow,
 * gated on the tab being visible, and gives up rather than retrying forever.
 */

const POLL_MS = 15_000;
// A browser that is gone answers the same way every time. Stop after this many
// consecutive failed attempts rather than retrying for the life of the
// session: the endpoint is proxied, and each attempt writes an error into the
// server log.
const MAX_FAILURES = 3;

interface UseBrowserPreviewOptions {
  adapter: Pick<AgentChatAdapter, "getBrowserPreviewSnapshot">;
  sessionId: string | null;
  /** False when the session has no browser: nothing to preview. */
  enabled: boolean;
}

export function useBrowserPreview({
  adapter,
  sessionId,
  enabled,
}: UseBrowserPreviewOptions): string | null {
  const [src, setSrc] = useState<string | null>(null);
  // Keeps the effect from re-subscribing when the adapter identity changes.
  const adapterRef = useRef(adapter);
  adapterRef.current = adapter;

  useEffect(() => {
    if (!enabled || !sessionId) {
      setSrc(null);
      return;
    }
    let cancelled = false;
    let failures = 0;
    let timer = 0;

    const refresh = async () => {
      // Skipped rather than queued: a hidden tab that comes back gets its
      // frame on the next tick, and never a burst of stale ones.
      if (document.hidden) return;
      const fetchSnapshot = adapterRef.current.getBrowserPreviewSnapshot;
      if (typeof fetchSnapshot !== "function") return;
      try {
        const snapshot = await fetchSnapshot(sessionId);
        if (cancelled) return;
        if (!snapshot) {
          // No browser to photograph any more; asking again will not change
          // the answer.
          window.clearInterval(timer);
          return;
        }
        failures = 0;
        // Keep the last good frame otherwise: blanking the card on a bad
        // refresh is worse than showing a slightly stale page.
        if (snapshot.src) setSrc(snapshot.src);
      } catch {
        if (cancelled) return;
        failures += 1;
        if (failures >= MAX_FAILURES) {
          window.clearInterval(timer);
        }
      }
    };

    void refresh();
    timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [enabled, sessionId]);

  return src;
}

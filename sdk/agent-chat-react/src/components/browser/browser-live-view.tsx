/// <reference path="../../novnc.d.ts" />
import { useEffect, useRef, useState } from "react";
import type { NoVncClient } from "@novnc/novnc";

interface BrowserLiveViewProps {
  src: string;
  testId?: string;
  /**
   * Called when the RFB connection drops *unexpectedly* (server close on
   * control-lease expiry, network blip, security failure) — not on intentional
   * unmount. ``clean`` mirrors noVNC's ``disconnect`` detail. The pane uses
   * this to drop local control state and fall back to the preview snapshot.
   */
  onDisconnect?: (clean: boolean) => void;
}

type ConnectionState = "connecting" | "connected" | "disconnected";

// The live-view URL is an http(s) asset path; noVNC needs the ws(s) scheme.
// Only http(s) is rewritten — an already-ws(s) URL is passed through so
// ws: (local/non-SSL) is not forced to wss:.
function toWsUrl(src: string): string {
  const url = new URL(src, window.location.href);
  if (url.protocol === "http:") {
    url.protocol = "ws:";
  } else if (url.protocol === "https:") {
    url.protocol = "wss:";
  }
  return url.toString();
}

export function BrowserLiveView({
  src,
  testId = "browser-rfb",
  onDisconnect,
}: BrowserLiveViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  // Keep the latest callback in a ref so the connection effect depends only on
  // ``src`` — a new ``onDisconnect`` identity must not tear down the session.
  const onDisconnectRef = useRef(onDisconnect);
  onDisconnectRef.current = onDisconnect;
  const [state, setState] = useState<ConnectionState>("connecting");

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    setState("connecting");

    let rfb: NoVncClient | undefined;
    let disposed = false;

    const handleConnect = () => setState("connected");
    const handleDisconnect = (event: Event) => {
      const detail = (event as CustomEvent<{ clean: boolean }>).detail;
      setState("disconnected");
      onDisconnectRef.current?.(detail?.clean ?? false);
    };

    // noVNC is browser-only (touches WebSocket/canvas at module load), so it is
    // imported lazily — the component stays safe to import in SSR/tests that do
    // not mount it.
    void import("@novnc/novnc")
      .then((mod) => {
        if (disposed || !containerRef.current) return;
        const instance = new mod.default(containerRef.current, toWsUrl(src), {
          wsProtocols: ["binary"],
        });
        instance.viewOnly = false;
        instance.scaleViewport = true;
        instance.addEventListener("connect", handleConnect);
        instance.addEventListener("disconnect", handleDisconnect);
        rfb = instance;
      })
      .catch(() => {
        if (!disposed) setState("disconnected");
      });

    return () => {
      // Detach listeners *before* disconnect() so an intentional unmount
      // (session change / control released) does not fire onDisconnect —
      // only externally-driven drops should.
      disposed = true;
      if (rfb) {
        rfb.removeEventListener("connect", handleConnect);
        rfb.removeEventListener("disconnect", handleDisconnect);
        rfb.disconnect();
      }
    };
  }, [src]);

  return (
    <div className="relative h-full w-full bg-black">
      <div ref={containerRef} data-testid={testId} className="h-full w-full" />
      {state !== "connected" && (
        <div
          data-testid="browser-rfb-overlay"
          className="pointer-events-none absolute inset-0 flex items-center justify-center bg-black/70 text-sm text-muted-foreground"
        >
          {state === "connecting"
            ? "Connecting to browser…"
            : "Live view disconnected — take control again to reconnect."}
        </div>
      )}
    </div>
  );
}

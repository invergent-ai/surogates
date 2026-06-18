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

// Zoom is driven through noVNC's own scaling, never a CSS transform: the RFB
// pointer mapping is ``absX(x) = x / display.scale``, so a CSS-transformed
// canvas would send clicks offset by the zoom factor. Instead we grow the RFB
// target element to ``pane × zoom`` inside an overflow-auto scroller and leave
// ``scaleViewport`` on — noVNC's ResizeObserver re-runs ``autoscale`` and
// re-derives ``display.scale``, keeping rendering *and* clicks correct while
// the scroller provides panning. Fit-to-pane is the floor (1×).
const MIN_ZOOM = 1;
const MAX_ZOOM = 3;
const ZOOM_STEP = 0.5;

function clampZoom(zoom: number): number {
  const bounded = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
  // Round to avoid floating-point drift accumulating across steps.
  return Math.round(bounded * 100) / 100;
}

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
  const [zoom, setZoom] = useState(MIN_ZOOM);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    setState("connecting");
    // A new session starts fit-to-pane; carrying a previous zoom over to an
    // unrelated framebuffer would be disorienting.
    setZoom(MIN_ZOOM);

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

  // Keep pointer-down on the controls from blurring the RFB canvas, which would
  // stop keyboard events from reaching the remote.
  const keepCanvasFocus = (event: React.MouseEvent) => event.preventDefault();
  const zoomTo = (next: number) => setZoom((current) => clampZoom(next || current));

  return (
    <div className="relative h-full w-full bg-black">
      <div className="absolute inset-0 overflow-auto">
        <div
          ref={containerRef}
          data-testid={testId}
          style={{ width: `${zoom * 100}%`, height: `${zoom * 100}%` }}
        />
      </div>
      {state === "connected" && (
        <div
          data-testid="browser-rfb-zoom"
          className="absolute bottom-3 right-3 flex items-center gap-0.5 rounded-md border border-line bg-card/90 p-0.5 text-muted-foreground shadow-sm backdrop-blur"
        >
          <button
            type="button"
            tabIndex={-1}
            aria-label="Zoom out"
            disabled={zoom <= MIN_ZOOM}
            onMouseDown={keepCanvasFocus}
            onClick={() => zoomTo(zoom - ZOOM_STEP)}
            className="flex size-6 items-center justify-center rounded text-sm leading-none hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none disabled:opacity-40"
          >
            −
          </button>
          <button
            type="button"
            tabIndex={-1}
            aria-label="Reset zoom"
            disabled={zoom === MIN_ZOOM}
            onMouseDown={keepCanvasFocus}
            onClick={() => setZoom(MIN_ZOOM)}
            className="min-w-10 rounded px-1 text-center text-xs tabular-nums hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none"
          >
            {Math.round(zoom * 100)}%
          </button>
          <button
            type="button"
            tabIndex={-1}
            aria-label="Zoom in"
            disabled={zoom >= MAX_ZOOM}
            onMouseDown={keepCanvasFocus}
            onClick={() => zoomTo(zoom + ZOOM_STEP)}
            className="flex size-6 items-center justify-center rounded text-sm leading-none hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none disabled:opacity-40"
          >
            +
          </button>
        </div>
      )}
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

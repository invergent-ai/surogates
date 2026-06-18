import { useEffect, useRef } from "react";
import RFB from "@novnc/novnc";

interface BrowserLiveViewProps {
  src: string;
  testId?: string;
}

// The live-view URL is an http(s) asset path; noVNC needs the ws(s) scheme.
function toWsUrl(src: string): string {
  const url = new URL(src, window.location.href);
  url.protocol = url.protocol === "http:" ? "ws:" : "wss:";
  return url.toString();
}

export function BrowserLiveView({
  src,
  testId = "browser-rfb",
}: BrowserLiveViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const rfb = new RFB(container, toWsUrl(src), { wsProtocols: ["binary"] });
    rfb.viewOnly = false;
    rfb.scaleViewport = true;
    return () => rfb.disconnect();
  }, [src]);

  return (
    <div
      ref={containerRef}
      data-testid={testId}
      className="h-full w-full bg-black"
    />
  );
}

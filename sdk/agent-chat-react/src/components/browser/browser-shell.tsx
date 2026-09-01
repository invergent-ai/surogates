import { useCallback, useEffect, useRef, useState } from "react";

/**
 * The agent browser, rendered as a browser rather than as a desktop.
 *
 * Frames arrive as binary JPEG on a WebSocket and commands go back as JSON.
 * Nothing CDP-shaped crosses this boundary: there is no message that expresses
 * JavaScript execution or a cookie read, which is the whole point of the
 * protocol this speaks.
 */

interface Tab {
  id: string;
  title: string;
  url: string;
  active: boolean;
}

interface BrowserShellProps {
  /** WebSocket URL of the session's shell endpoint (http(s) is rewritten). */
  src: string;
  /** Whether this viewer holds the control lease. The server gates too. */
  hasControl: boolean;
  onToggleControl?: () => void;
  onClose?: () => void;
  onMaximize?: () => void;
  /**
   * Called when the socket drops *unexpectedly* — not on unmount, so an
   * intentional teardown never looks like a failure to the pane.
   */
  onDisconnect?: (clean: boolean) => void;
  testId?: string;
}

type Connection = "connecting" | "connected" | "disconnected";

// Keys the protocol accepts. Mirrors NAMED_KEYS in surogates/browser/shell.py:
// letters and function keys are absent on purpose, so no Ctrl+Shift+I and no
// F12. Typed text goes as `type`, never as a key event.
const NAMED_KEYS = new Set([
  "Enter", "Tab", "Escape", "Backspace", "Delete",
  "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
  "Home", "End", "PageUp", "PageDown",
]);

/** The live-view URL is an http(s) asset path; the socket needs ws(s). */
function toWsUrl(src: string): string {
  const url = new URL(src, window.location.href);
  if (url.protocol === "http:") url.protocol = "ws:";
  else if (url.protocol === "https:") url.protocol = "wss:";
  return url.toString();
}

/**
 * Map a pointer position to a 0-1 point inside the rendered frame.
 *
 * `object-fit: contain` letterboxes, so the painted image is smaller than its
 * element. Normalizing against the element box would drift by the letterbox,
 * and clamping would turn a click on the background into a click on the page's
 * edge — so a point outside the image returns null and is dropped.
 *
 * Exported for tests: this is the client half of the coordinate contract the
 * server's e2e suite checks from the other side.
 */
export function normalizePoint(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number },
  naturalWidth: number,
  naturalHeight: number,
): { x: number; y: number } | null {
  let { width, height } = rect;
  let offsetX = 0;
  let offsetY = 0;
  if (naturalWidth > 0 && naturalHeight > 0) {
    const scale = Math.min(rect.width / naturalWidth, rect.height / naturalHeight);
    width = naturalWidth * scale;
    height = naturalHeight * scale;
    offsetX = (rect.width - width) / 2;
    offsetY = (rect.height - height) / 2;
  }
  if (width <= 0 || height <= 0) return null;
  const x = (clientX - rect.left - offsetX) / width;
  const y = (clientY - rect.top - offsetY) / height;
  if (x < 0 || x > 1 || y < 0 || y > 1) return null;
  return { x, y };
}

function hostOf(url: string): { origin: string; rest: string } {
  try {
    const parsed = new URL(url);
    return { origin: parsed.host, rest: `${parsed.pathname}${parsed.search}` };
  } catch {
    return { origin: url, rest: "" };
  }
}

const ICON = "flex size-[26px] shrink-0 items-center justify-center rounded-[5px] " +
  "text-muted-foreground transition-colors hover:bg-secondary " +
  "hover:text-foreground disabled:pointer-events-none disabled:opacity-40";

export function BrowserShell({
  src,
  hasControl,
  onToggleControl,
  onClose,
  onMaximize,
  onDisconnect,
  testId = "browser-shell",
}: BrowserShellProps) {
  const socketRef = useRef<WebSocket | null>(null);
  const frameRef = useRef<HTMLImageElement>(null);
  const onDisconnectRef = useRef(onDisconnect);
  onDisconnectRef.current = onDisconnect;
  const objectUrlRef = useRef<string | null>(null);

  const [state, setState] = useState<Connection>("connecting");
  const [frame, setFrame] = useState<string | null>(null);
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [nav, setNav] = useState({ url: "", title: "" });
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    setState("connecting");
    let disposed = false;
    const socket = new WebSocket(toWsUrl(src));
    socketRef.current = socket;

    socket.onopen = () => {
      if (!disposed) setState("connected");
    };
    socket.onerror = () => {
      if (!disposed) setState("disconnected");
    };
    socket.onclose = (event: { wasClean?: boolean }) => {
      if (disposed) return;
      setState("disconnected");
      onDisconnectRef.current?.(event?.wasClean ?? false);
    };
    socket.onmessage = (event: { data: unknown }) => {
      if (disposed) return;
      const data = event.data;
      if (typeof data !== "string") {
        // A frame. Revoke the previous URL or every frame leaks one.
        const next = URL.createObjectURL(
          data instanceof Blob ? data : new Blob([data as ArrayBuffer]),
        );
        if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
        objectUrlRef.current = next;
        setFrame(next);
        return;
      }
      let message: Record<string, unknown>;
      try {
        message = JSON.parse(data);
      } catch {
        return;
      }
      if (message.t === "tabs") setTabs((message.tabs as Tab[]) ?? []);
      else if (message.t === "nav") {
        setNav({
          url: String(message.url ?? ""),
          title: String(message.title ?? ""),
        });
      }
    };

    return () => {
      // Detach before closing so an intentional unmount does not look like a
      // drop — only externally-driven closes reach onDisconnect.
      disposed = true;
      socket.onopen = null;
      socket.onmessage = null;
      socket.onclose = null;
      socket.onerror = null;
      socket.close();
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = null;
      socketRef.current = null;
    };
  }, [src]);

  const send = useCallback((message: Record<string, unknown>) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify(message));
  }, []);

  /** Commands need the lease. The server enforces this too; this keeps the
   *  UI from sending what it knows will be dropped. */
  const command = useCallback(
    (message: Record<string, unknown>) => {
      if (!hasControl) return;
      send(message);
    },
    [hasControl, send],
  );

  const pointerToPage = (event: React.MouseEvent | React.WheelEvent) => {
    const image = frameRef.current;
    if (!image) return null;
    return normalizePoint(
      event.clientX,
      event.clientY,
      image.getBoundingClientRect(),
      image.naturalWidth,
      image.naturalHeight,
    );
  };

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (!hasControl) return;
    if (NAMED_KEYS.has(event.key)) {
      event.preventDefault();
      command({
        t: "key",
        key: event.key,
        mods: {
          shift: event.shiftKey,
          ctrl: event.ctrlKey,
          alt: event.altKey,
          meta: event.metaKey,
        },
      });
      return;
    }
    // A single printable character is text, not a command key.
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      command({ t: "type", text: event.key });
    }
  };

  const { origin, rest } = hostOf(nav.url);
  const showTabs = tabs.length > 1;

  return (
    <div
      data-testid={testId}
      className="flex h-full min-h-0 flex-col bg-background"
    >
      <div className="flex min-h-11 items-center gap-1.5 border-b border-line bg-card px-2.5">
        <button
          type="button"
          aria-label="Back"
          className={ICON}
          disabled={!hasControl}
          onClick={() => command({ t: "back" })}
        >
          <ChevronLeft />
        </button>
        <button
          type="button"
          aria-label="Forward"
          className={ICON}
          disabled={!hasControl}
          onClick={() => command({ t: "forward" })}
        >
          <ChevronRight />
        </button>
        <button
          type="button"
          aria-label="Reload"
          className={ICON}
          disabled={!hasControl}
          onClick={() => command({ t: "reload" })}
        >
          <Reload />
        </button>
        <button
          type="button"
          data-testid="browser-shell-control"
          aria-label={hasControl ? "Return control" : "Take control"}
          aria-pressed={hasControl}
          onClick={onToggleControl}
          className={
            hasControl
              ? "flex size-[26px] shrink-0 items-center justify-center rounded-[5px] bg-primary text-primary-foreground"
              : ICON
          }
        >
          {/* One glyph in both states: a second, circular one beside Reload
              would read as another refresh button. Colour is the mode. */}
          <Pointer />
        </button>
        <div
          data-testid="browser-shell-url"
          className="flex h-7 min-w-0 flex-grow items-center gap-1.5 rounded-md border border-line bg-background px-2.5 text-[11px] text-foreground"
        >
          <span
            aria-hidden="true"
            className={`size-1.5 shrink-0 rounded-full ${
              state === "connected" ? "bg-emerald-500" : "bg-muted-foreground"
            }`}
          />
          <span className="truncate">
            {origin}
            <span className="text-muted-foreground">{rest}</span>
          </span>
        </div>
        <div className="relative">
          <button
            type="button"
            aria-label="More"
            className={ICON}
            onClick={() => setMenuOpen((open) => !open)}
          >
            <Ellipsis />
          </button>
          {menuOpen && (
            <div
              data-testid="browser-shell-menu"
              className="absolute right-0 top-8 z-10 min-w-36 rounded-md border border-line bg-card py-1 text-xs shadow-md"
            >
              {onMaximize && (
                <button
                  type="button"
                  className="block w-full px-3 py-1.5 text-left hover:bg-secondary"
                  onClick={() => {
                    setMenuOpen(false);
                    onMaximize();
                  }}
                >
                  Maximize
                </button>
              )}
              {onClose && (
                <button
                  type="button"
                  className="block w-full px-3 py-1.5 text-left hover:bg-secondary"
                  onClick={() => {
                    setMenuOpen(false);
                    onClose();
                  }}
                >
                  Close browser
                </button>
              )}
            </div>
          )}
        </div>
      </div>

      {showTabs && (
        <div
          data-testid="browser-shell-tabs"
          className="flex min-h-[34px] items-center gap-0.5 border-b border-line bg-card px-1.5"
        >
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              data-testid="browser-shell-tab"
              onClick={() => send({ t: "switch_tab", id: tab.id })}
              className={`flex h-6 max-w-33 items-center gap-1.5 truncate rounded px-2 text-[11px] ${
                tab.active
                  ? "bg-secondary text-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              <span className="truncate">{tab.title || tab.url}</span>
            </button>
          ))}
        </div>
      )}

      <div
        className={`relative min-h-0 flex-1 bg-black ${
          hasControl ? "ring-2 ring-inset ring-primary" : ""
        }`}
        tabIndex={hasControl ? 0 : -1}
        onKeyDown={handleKeyDown}
      >
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
        <img
          ref={frameRef}
          data-testid="browser-shell-frame"
          alt=""
          src={frame ?? undefined}
          draggable={false}
          className="h-full w-full object-contain"
          onMouseDown={(event) => {
            const point = pointerToPage(event);
            if (point) command({ t: "click", ...point, count: event.detail || 1 });
          }}
          onWheel={(event) => {
            const point = pointerToPage(event);
            if (point) {
              command({
                t: "scroll",
                ...point,
                dx: event.deltaX,
                dy: event.deltaY,
              });
            }
          }}
        />
        {state !== "connected" && (
          <div
            data-testid="browser-shell-overlay"
            className="pointer-events-none absolute inset-0 flex items-center justify-center bg-black/70 text-sm text-muted-foreground"
          >
            {state === "connecting"
              ? "Connecting to browser…"
              : "Browser disconnected."}
          </div>
        )}
        {hasControl && (
          <div className="pointer-events-none absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full bg-black/85 px-3 py-1 text-[11px] font-medium text-amber-200">
            <Pointer />
            You have control · click the pointer to return
          </div>
        )}
      </div>
    </div>
  );
}

/* Icons: 14px lucide paths, matching the set the rest of the pane uses. */
const svg = {
  width: 14,
  height: 14,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

const ChevronLeft = () => (
  <svg {...svg}>
    <path d="m15 18-6-6 6-6" />
  </svg>
);

const ChevronRight = () => (
  <svg {...svg}>
    <path d="m9 18 6-6-6-6" />
  </svg>
);

const Reload = () => (
  <svg {...svg}>
    <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
    <path d="M3 3v5h5" />
  </svg>
);

const Pointer = () => (
  <svg {...svg}>
    <path d="M12.586 12.586 19 19" />
    <path d="M3.688 3.037a.497.497 0 0 0-.651.651l6.5 15.999a.501.501 0 0 0 .947-.062l1.569-6.083a2 2 0 0 1 1.448-1.479l6.124-1.579a.5.5 0 0 0 .063-.947z" />
  </svg>
);

const Ellipsis = () => (
  <svg {...svg}>
    <circle cx="12" cy="12" r="1" />
    <circle cx="19" cy="12" r="1" />
    <circle cx="5" cy="12" r="1" />
  </svg>
);

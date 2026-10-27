import { Button } from "@invergent/ui-sdk/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@invergent/ui-sdk/dropdown-menu";
import { TabStrip, TabStripTab } from "@invergent/ui-sdk/tab-strip";
import { Toggle } from "@invergent/ui-sdk/toggle";
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
   * Called when the connection ends for good: the server closed it with a
   * code that means the browser is gone or refused, never on a drop the
   * shell reconnects from, and never on unmount.
   */
  onDisconnect?: (clean: boolean) => void;
  testId?: string;
}

type Connection = "connecting" | "connected" | "reconnecting" | "closed" | "disconnected";

// Close codes after which the browser is still there to come back to: an
// abnormal drop, a server error, the shell failing (4500), the browser not
// answering in time (4502), or its CDP connection dying under the viewer (4503).
const RECONNECT_CODES = new Set([1006, 1011, 4500, 4502, 4503]);
// The browser is gone: the session has none, or it was closed.
const CLOSED_CODE = 4404;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 15000;

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
  // Whether this connection has painted a frame yet. The last frame of an
  // earlier connection stays up underneath, dimmed, until a new one lands.
  const [framed, setFramed] = useState(false);
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [nav, setNav] = useState({ url: "", title: "" });
  // The address bar is editable: the user types a URL and presses Enter to
  // navigate (the server protocol carries `navigate`). While the field is
  // focused, server-side updates must not clobber what is being typed —
  // a nav event arriving mid-typing would otherwise rewrite the text.
  const [address, setAddress] = useState("");
  const editingRef = useRef(false);

  useEffect(() => {
    let disposed = false;
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const connect = () => {
      const current = new WebSocket(toWsUrl(src));
      socket = current;
      socketRef.current = current;
      setFramed(false);

      current.onopen = () => {
        if (disposed) return;
        attempt = 0;
        setState("connected");
      };
      current.onclose = (event: { code?: number; wasClean?: boolean }) => {
        if (disposed) return;
        const code = event?.code ?? 1006;
        if (RECONNECT_CODES.has(code)) {
          setState("reconnecting");
          const delay = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS);
          attempt += 1;
          retry = setTimeout(connect, delay);
          return;
        }
        setState(code === CLOSED_CODE ? "closed" : "disconnected");
        onDisconnectRef.current?.(event?.wasClean ?? false);
      };
      current.onmessage = (event: { data: unknown }) => {
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
          setFramed(true);
          return;
        }
        let message: Record<string, unknown>;
        try {
          message = JSON.parse(data);
        } catch {
          return;
        }
        if (message.t === "tabs") {
          const next = (message.tabs as Tab[]) ?? [];
          setTabs(next);
          // The tabs snapshot is the only address the server sends at connect
          // and after a tab switch — nav messages only arrive on navigation.
          const active = next.find((tab) => tab.active);
          if (active) {
            setNav({ url: active.url, title: active.title });
          }
        } else if (message.t === "nav") {
          setNav({
            url: String(message.url ?? ""),
            title: String(message.title ?? ""),
          });
        } else if (message.t === "status" && message.state === "busy") {
          // The page is not answering yet; frames resume when it does.
          setFramed(false);
        }
      };
    };

    setState("connecting");
    connect();
    return () => {
      // Detach before closing so an intentional unmount does not look like a
      // drop — only externally-driven closes reach onDisconnect.
      disposed = true;
      if (retry) clearTimeout(retry);
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onclose = null;
        socket.onerror = null;
        socket.close();
      }
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = null;
      socketRef.current = null;
    };
  }, [src]);

  // Server-driven address sync: the tabs snapshot and nav messages are the
  // truth about where the tab is. Skipped while the user is editing, so a
  // mid-typing navigation cannot rewrite their text.
  useEffect(() => {
    if (!editingRef.current) setAddress(nav.url);
  }, [nav.url]);

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

  const showTabs = tabs.length > 1;

  return (
    <div
      data-testid={testId}
      className="flex h-full min-h-0 flex-col bg-background"
    >
      <div className="flex min-h-11 items-center gap-1.5 border-b border-line bg-card px-2.5">
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Back"
          disabled={!hasControl}
          onClick={() => command({ t: "back" })}
        >
          <ChevronLeft />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Forward"
          disabled={!hasControl}
          onClick={() => command({ t: "forward" })}
        >
          <ChevronRight />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Reload"
          disabled={!hasControl}
          onClick={() => command({ t: "reload" })}
        >
          <Reload />
        </Button>
        {/* Offered where it takes or returns control, or says the viewer holds it. */}
        {(onToggleControl || hasControl) && (
          <Toggle
            size="icon-xs"
            data-testid="browser-shell-control"
            aria-label={hasControl ? "Return control" : "Take control"}
            pressed={hasControl}
            onPressedChange={() => onToggleControl?.()}
          >
            {/* One glyph in both states: a second, circular one beside Reload
                would read as another refresh button. Colour is the mode. */}
            <Pointer />
          </Toggle>
        )}
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
          <input
            type="text"
            aria-label="Address"
            placeholder="Type a URL and press Enter"
            className="h-full min-w-0 flex-grow bg-transparent outline-none placeholder:text-muted-foreground"
            disabled={!hasControl}
            value={address}
            onChange={(event) => setAddress(event.target.value)}
            onFocus={() => {
              editingRef.current = true;
            }}
            onBlur={() => {
              editingRef.current = false;
              setAddress(nav.url);
            }}
            onKeyDown={(event) => {
              if (event.key !== "Enter") return;
              event.preventDefault();
              (event.target as HTMLInputElement).blur();
              const typed = address.trim();
              if (!typed) return;
              // The server rejects anything without an http(s) scheme, so a
              // bare domain is completed rather than silently dropped.
              const url = /^[a-z][a-z0-9+.-]*:\/\//i.test(typed)
                ? typed
                : `https://${typed}`;
              setAddress(url);
              command({ t: "navigate", url });
            }}
          />
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="ghost" size="icon-xs" aria-label="More">
              <Ellipsis />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent data-testid="browser-shell-menu" align="end">
            {onMaximize && (
              <DropdownMenuItem onSelect={onMaximize}>Maximize</DropdownMenuItem>
            )}
            {onClose && (
              <DropdownMenuItem onSelect={onClose}>Close browser</DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {showTabs && (
        <TabStrip
          data-testid="browser-shell-tabs"
          className="min-h-[34px] border-b border-line bg-card px-1.5"
        >
          {tabs.map((tab) => (
            <TabStripTab
              key={tab.id}
              active={tab.active}
              data-testid="browser-shell-tab"
              onSelect={() => send({ t: "switch_tab", id: tab.id })}
              onClose={hasControl ? () => command({ t: "close_tab", id: tab.id }) : undefined}
              closeLabel={`Close ${tab.title || tab.url}`}
            >
              {tab.title || tab.url}
            </TabStripTab>
          ))}
        </TabStrip>
      )}

      <div
        className={`relative min-h-0 flex-1 bg-black ${
          hasControl ? "ring-2 ring-inset ring-[#F5A524]" : ""
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
        {(state !== "connected" || !framed) && (
          <div
            data-testid="browser-shell-overlay"
            className="pointer-events-none absolute inset-0 flex items-center justify-center bg-black/70 text-sm text-muted-foreground"
          >
            {OVERLAY[state === "connected" ? "waiting" : state]}
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

const OVERLAY: Record<Exclude<Connection, "connected"> | "waiting", string> = {
  connecting: "Connecting to browser…",
  reconnecting: "Reconnecting…",
  waiting: "Waiting for the page…",
  closed: "Browser closed.",
  disconnected: "Browser disconnected.",
};

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
  // A size class of its own, so a small Button's default svg size (12px)
  // leaves the toolbar's 14px glyphs alone.
  className: "size-3.5",
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

import { Brain, Send } from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { cn } from "../../lib/utils";
import { useAgentChatRuntime } from "../../runtime/use-agent-chat-runtime";
import type {
  AgentChatAdapter,
  AgentChatRuntimeApi,
  AgentChatViewMode,
} from "../../types";
import { Button } from "../ui/button";
import { Spinner } from "../ui/spinner";
import {
  type AtlasExtras,
  type Rect,
  atlasMetadata,
  boardMarks,
  buildAtlas,
  contentBeyond,
  contentBounds,
  inkHeight,
  mapHotspots,
  OCCUPANCY_GRID,
  occupancyCells,
  paintMarks,
  planAtlas,
} from "./atlas";
import {
  type WbDoc,
  type WbObject,
  emptyDoc,
  foldToolCalls,
  makeTextObject,
  mapSelected,
  scaleObject,
  translateObject,
} from "./doc";
import { FormulaCache } from "./formula";
import {
  StrokeBuilder,
  logicalToScreen,
  panBy,
  screenToLogical,
  strokePointsFromEvent,
  zoomAt,
  zoomFactorFromWheel,
  zoomToFit,
} from "./input";
import { makeCommandResolver } from "./layout";
import {
  latestBoardSession,
  loadDoc,
  shouldReloadCanvas,
  useDebouncedSave,
} from "./persist";
import {
  type RenderServices,
  type View,
  handleAt,
  hitTest,
  objectBounds,
  objectsInRect,
  oppositeCorner,
  rectFromCorners,
  renderDoc,
  selectionBounds,
} from "./render";
import { ArtifactBlock } from "../chat/artifacts/artifact-block";
import type { AgentChatArtifactKind } from "../../types";
import { INK_COLORS, INK_WIDTHS, type WbTool, ToolRail } from "./tool-rail";

export type { WbTool };

/** Undo depth. PenEcho's MAX_HISTORY, and for the same reason: deeper
 *  costs memory nobody spends. */
const MAX_HISTORY = 30;

/** Where a fresh board opens: the origin, centred. The canvas is
 *  infinite, so there is no corner to start from and no reason to
 *  prefer one direction. */
const INITIAL_VIEW: View = { x: -400, y: -300, zoom: 1 };

/** Default size for user-typed text. The agent picks its own. */
/** The eraser is chunkier than the pen at the same width setting. */
const ERASER_SCALE = 4;

const TEXT_FONT_SIZE = 24;
const TEXT_MAX_WIDTH = 320;

/** How long a canvas load may take before the board says so. */
const RESTORE_HINT_DELAY_MS = 250;

/** `PointerEvent.button` for the middle button / wheel click. */
const MIDDLE_BUTTON = 1;

/** Mirrors the composer's segments so the two switches read alike. */
const VIEW_MODE_LABELS: Record<AgentChatViewMode, string> = {
  simple: "Simple",
  expert: "Advanced",
  whiteboard: "Whiteboard",
};

export interface AgentWhiteboardProps {
  adapter: AgentChatAdapter;
  agentId?: string;
  sessionId: string | null;
  onSessionChange?: (sessionId: string) => void;
  /**
   * Navigate to a session-less board. Without it "New board" cannot
   * clear the session id, which only the host's router owns.
   */
  onNewBoard?: () => void;
  disabled?: boolean;
  /**
   * View-mode switch, when the board is hosted inside `AgentChat`. The
   * control lives in the chat composer, so without it here the board is
   * a room with no door back.
   */
  viewMode?: AgentChatViewMode;
  onViewModeChange?: (mode: AgentChatViewMode) => void;
}

/**
 * The board over an existing runtime.
 *
 * Split from the standalone export so `AgentChat` can host it on the
 * runtime it already has: two `useAgentChatRuntime` calls for one
 * session would open two event streams and double every applied event.
 */
export interface WhiteboardSurfaceProps
  extends Omit<AgentWhiteboardProps, "adapter" | "agentId"> {
  adapter: AgentChatAdapter;
  agentId?: string;
  runtime: AgentChatRuntimeApi;
}

/** The board, standalone: owns its runtime. */
export function AgentWhiteboard(props: AgentWhiteboardProps) {
  const runtime = useAgentChatRuntime({
    adapter: props.adapter,
    agentId: props.agentId,
    sessionId: props.sessionId,
    onSessionChange: props.onSessionChange,
  });
  return <WhiteboardSurface {...props} runtime={runtime} />;
}

export function WhiteboardSurface({
  adapter,
  agentId,
  sessionId,
  onSessionChange,
  onNewBoard,
  disabled,
  viewMode,
  onViewModeChange,
  runtime,
}: WhiteboardSurfaceProps) {

  const [doc, setDoc] = useState<WbDoc>(emptyDoc);
  const [view, setView] = useState<View>(INITIAL_VIEW);
  const [tool, setTool] = useState<WbTool>("pen");
  const [color, setColor] = useState<string>(INK_COLORS[0]);
  const [width, setWidth] = useState<number>(INK_WIDTHS[1]);
  const [question, setQuestion] = useState("");
  // An open text editor, positioned in logical space. Null when closed.
  const [editor, setEditor] = useState<{ x: number; y: number } | null>(null);
  const [editorText, setEditorText] = useState("");
  const [marquee, setMarquee] = useState<
    { from: { x: number; y: number }; to: { x: number; y: number } } | null
  >(null);
  // Set by "New board" so the resume effect does not immediately pull
  // the user back into the board they just left.
  const [wantFresh, setWantFresh] = useState(false);
  // Which button started the turn in flight. `deep` is many
  // round-trips and worth naming; `sketch` is one and should not
  // advertise a wait that is about to be over.
  const [askMode, setAskMode] = useState<"sketch" | "deep">("sketch");

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });

  // Undo/redo stacks of whole documents. Cheap because objects are
  // shared structurally — only the array wrapper is copied.
  const undoStack = useRef<WbDoc[]>([]);
  const redoStack = useRef<WbDoc[]>([]);
  const [historyTick, setHistoryTick] = useState(0);

  // Input captured since the last Ask: what the model is told to attend
  // to. Refs, not state — they change per pointer sample and must not
  // re-render the tree.
  const strokeRef = useRef<StrokeBuilder | null>(null);
  const panFrom = useRef<{ x: number; y: number } | null>(null);
  // Live drag / resize of the selection. Held in refs and committed on
  // pointerup, so one gesture is one undo step rather than one per
  // pointer sample.
  const dragFrom = useRef<{ x: number; y: number } | null>(null);
  // Pointer position in logical space, for the eraser ring. A ref, not
  // state: it changes on every pointer sample and must not re-render.
  const cursorRef = useRef<{ x: number; y: number } | null>(null);
  const resize = useRef<{
    anchor: { x: number; y: number };
    start: { x: number; y: number };
  } | null>(null);
  // The document as it stood before the current gesture. Undo has to
  // step back to this, not to the last pointer sample.
  const gestureBaseline = useRef<WbDoc | null>(null);
  const hotspotsRef = useRef<{ x: number; y: number }[]>([]);
  // Text the user typed since the last Ask. Sent verbatim as
  // transcription ground truth so the model never has to read its own
  // rendering of it back out of the atlas.
  const typedRef = useRef<string[]>([]);
  const dirtyRef = useRef<Rect | null>(null);
  // Object ids as they stood at the last Ask. What is local and not in
  // here is what the user has added since -- which is how ink drawn
  // around one of the agent's answers is told apart from ink that was
  // always there.
  const seenAtLastAsk = useRef<Set<string>>(new Set());

  const repaintRef = useRef<() => void>(() => undefined);
  const formulaCache = useMemo(
    () => new FormulaCache(() => repaintRef.current()),
    [],
  );

  const services = useMemo<RenderServices>(
    () => ({
      formula: (latex, fontSize) => formulaCache.measure(latex, fontSize),
      formulaImage: (latex, fontSize) => formulaCache.get(latex, fontSize),
      createCanvas: (w, h) => {
        const c = document.createElement("canvas");
        c.width = w;
        c.height = h;
        return c;
      },
    }),
    [formulaCache],
  );

  // Resolves anchored draw commands ("right of the latest ink") into
  // coordinates at fold time, against the board as it is then.
  const resolveDraw = useMemo(() => makeCommandResolver(services), [services]);

  // ------------------------------------------------------------------
  // Document lifecycle
  // ------------------------------------------------------------------

  // A board lives in a session, and this route carries no session id
  // until one exists. Without resuming, leaving the board and coming
  // back lands on a blank canvas and silently starts a new one, with no
  // route back to what was drawn.
  // A session this board adopted by resuming rather than by creating.
  // Both reach the loader below as `null -> id`, but only the created
  // one carries strokes that predate the session and would be destroyed
  // by a load.
  const resumedSession = useRef<string | null>(null);

  useEffect(() => {
    if (sessionId || wantFresh || !onSessionChange) return;
    let cancelled = false;
    void latestBoardSession(adapter, agentId).then((id) => {
      if (!cancelled && id) {
        resumedSession.current = id;
        onSessionChange(id);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [adapter, agentId, sessionId, wantFresh, onSessionChange]);

  // The session whose canvas is actually on screen. Compared against the
  // incoming prop so adopting a just-created session is distinguishable
  // from switching to a different one.
  //
  // Claimed only once a load commits, never when one is merely started.
  // StrictMode runs every effect twice — mount, clean up, mount again —
  // so a run that claims the session up front and is then cancelled
  // leaves the second run looking at `previous === next`, skipping the
  // load entirely and showing a board holding nothing but the agent's
  // replayed objects.
  const loadedSession = useRef<string | null | undefined>(undefined);

  // Whether the canvas on screen reflects the stored one yet.  Autosave
  // is held until it does.
  //
  // A remount starts from `emptyDoc()`, and the fold effect below
  // immediately replays the agent's draw calls onto it -- so for the
  // moment before the load resolves, the live document is a real,
  // non-empty board holding the agent's objects and none of the user's.
  // Saving in that window writes it over the stored board, destroying
  // the ink the load was about to restore, and the load then returns
  // what was just written.  That is why the loss needed an agent reply
  // to reproduce: with nothing to fold the document stays empty, and
  // `useDebouncedSave` already declines to save an empty one.
  const [canvasReady, setCanvasReady] = useState(false);

  // The veil is shown only once a load is slow enough to be worth
  // mentioning.  A warm switch back resolves well inside this, and a
  // spinner flashing on every toggle of the view is worse than showing
  // nothing at all.  Blocking input is NOT delayed -- that has to hold
  // from the first frame, or the strokes it exists to protect are the
  // ones that get eaten.
  const [restoringVisible, setRestoringVisible] = useState(false);
  useEffect(() => {
    if (canvasReady) {
      setRestoringVisible(false);
      return;
    }
    const timer = setTimeout(
      () => setRestoringVisible(true),
      RESTORE_HINT_DELAY_MS,
    );
    return () => clearTimeout(timer);
  }, [canvasReady]);

  useEffect(() => {
    // Once a session exists again the "fresh" intent is spent; without
    // clearing it, every later return to the board starts blank.
    if (sessionId && wantFresh) setWantFresh(false);
    const previous = loadedSession.current;

    if (
      !shouldReloadCanvas(previous, sessionId, {
        resumed: resumedSession.current === sessionId,
      })
    ) {
      loadedSession.current = sessionId;
      // Clearing only on a real change: `null -> id` is this board
      // adopting the session its own first Ask created, and everything
      // drawn before that question is still the live document.
      if (!sessionId && previous) setDoc(emptyDoc());
      // Nothing to wait for: the document on screen is the live one.
      setCanvasReady(true);
      return;
    }

    // Switching to a different session re-arms the gate, so the outgoing
    // board's document can never be saved under the incoming session's id.
    setCanvasReady(false);
    let cancelled = false;
    void loadDoc(
      adapter,
      sessionId as string,
      runtime.messages,
      resolveDraw,
    ).then(
      (loaded) => {
        if (cancelled) return;
        loadedSession.current = sessionId;
        setDoc(loaded);
        setCanvasReady(true);
      },
    );
    return () => {
      cancelled = true;
    };
    // Deliberately not depending on runtime.messages: this is the load
    // for a session change, and the fold effect below keeps it current.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adapter, sessionId]);

  // Held until the stored board is in hand.  Folding onto the empty
  // document a remount starts from would paint the agent's objects
  // alone -- a board the user never had, shown for as long as the fetch
  // takes and then replaced.  `loadDoc` folds the same calls itself, so
  // nothing is skipped by waiting; the load simply arrives complete.
  useEffect(() => {
    if (!canvasReady) return;
    setDoc((d) => foldToolCalls(d, runtime.messages, resolveDraw));
  }, [runtime.messages, canvasReady, resolveDraw]);

  // A null session id is what `useDebouncedSave` already treats as
  // "nothing to write to", so the gate reuses it rather than growing a
  // second way to say the same thing.
  useDebouncedSave(adapter, canvasReady ? sessionId : null, doc);

  // The transcript views are only worth offering once there is a
  // transcript. Before the agent's first answer, switching to Simple or
  // Advanced opens an empty thread -- or, on a board that has not been
  // asked anything yet, no session at all.
  const hasAnswer = useMemo(
    () => runtime.messages.some((m) => m.role === "assistant"),
    [runtime.messages],
  );

  // ------------------------------------------------------------------
  // History
  // ------------------------------------------------------------------

  /** Record *snapshot* as the state undo should return to. */
  const pushHistory = useCallback((snapshot: WbDoc) => {
    undoStack.current.push(snapshot);
    if (undoStack.current.length > MAX_HISTORY) undoStack.current.shift();
    redoStack.current = [];
    setHistoryTick((t) => t + 1);
  }, []);

  const commit = useCallback(
    (next: (prev: WbDoc) => WbDoc) => {
      setDoc((prev) => {
        pushHistory(prev);
        return next(prev);
      });
    },
    [pushHistory],
  );

  const undo = useCallback(() => {
    setDoc((prev) => {
      const previous = undoStack.current.pop();
      if (!previous) return prev;
      redoStack.current.push(prev);
      setHistoryTick((t) => t + 1);
      return previous;
    });
  }, []);

  const redo = useCallback(() => {
    setDoc((prev) => {
      const next = redoStack.current.pop();
      if (!next) return prev;
      undoStack.current.push(prev);
      setHistoryTick((t) => t + 1);
      return next;
    });
  }, []);

  const deleteSelected = useCallback(() => {
    commit((prev) => ({
      ...prev,
      objects: prev.objects.filter((o) => !o.selected),
    }));
  }, [commit]);

  // ------------------------------------------------------------------
  // Sizing and painting
  // ------------------------------------------------------------------

  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      setSize({ w: el.clientWidth || 800, h: el.clientHeight || 600 });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // The frame the next Ask will capture. Memoised because it walks every
  // object, and paint runs on every pointer sample. `latest` is left out
  // deliberately: it lives in a ref, so it could not invalidate this,
  // and while the board fits the frame it makes no difference anyway.
  const gridRect = useMemo(
    () => planAtlas(doc, null, view, size, services).sourceRect,
    [doc, view, size, services],
  );
  // The same labels the agent will be sent, so the user can read "A3"
  // off the board and use it in a question.
  const liveMarks = useMemo(
    () => boardMarks(doc, services, { unit: inkHeight(doc, services) ?? 40 }),
    [doc, services],
  );

  const paint = useCallback(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const dpr = globalThis.devicePixelRatio || 1;
    if (canvas.width !== size.w * dpr || canvas.height !== size.h * dpr) {
      canvas.width = size.w * dpr;
      canvas.height = size.h * dpr;
    }
    // One transform for everything: renderDoc folds the ratio in and
    // leaves it set, so the live stroke and the marquee below paint in
    // the same logical space the committed objects did. Anything drawn
    // in a different space jumps the moment it is committed.
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.w, size.h);
    renderDoc(ctx, doc, view, size, services, dpr);

    // The same cells the agent is sent, drawn under the ink so the two
    // of you are looking at one reference frame: "the answer goes in
    // 9,6" means the same square to both. Painted here rather than
    // written to the document, so it can never reach the atlas twice or
    // be mistaken for something drawn.
    if (gridRect) {
      const px = 1 / (view.zoom * dpr);
      const cw = gridRect.w / OCCUPANCY_GRID;
      const ch = gridRect.h / OCCUPANCY_GRID;
      ctx.save();
      ctx.strokeStyle = "rgba(37, 99, 235, 0.16)";
      ctx.lineWidth = px;
      ctx.beginPath();
      for (let i = 0; i <= OCCUPANCY_GRID; i++) {
        ctx.moveTo(gridRect.x + i * cw, gridRect.y);
        ctx.lineTo(gridRect.x + i * cw, gridRect.y + gridRect.h);
        ctx.moveTo(gridRect.x, gridRect.y + i * ch);
        ctx.lineTo(gridRect.x + gridRect.w, gridRect.y + i * ch);
      }
      ctx.stroke();
      // Counter-scaled so the labels stay a constant size on screen
      // however far the board is zoomed.
      ctx.fillStyle = "rgba(37, 99, 235, 0.5)";
      ctx.font = `${11 / view.zoom}px system-ui, sans-serif`;
      ctx.textBaseline = "top";
      for (let i = 0; i < OCCUPANCY_GRID; i++) {
        ctx.fillText(String(i), gridRect.x + i * cw + 3 * px, gridRect.y + 2 * px);
        if (i > 0) {
          ctx.fillText(String(i), gridRect.x + 3 * px, gridRect.y + i * ch + 2 * px);
        }
      }
      ctx.restore();
    }
    if (liveMarks.length > 0) {
      const px = 1 / (view.zoom * dpr);
      paintMarks(ctx, liveMarks, (r) => r, 12 / view.zoom, px * 1.5);
    }

    // The marquee is interface, not content: painted here in screen
    // space and never written to the document, so it can never reach
    // the atlas and be mistaken for something the user drew.
    if (marquee) {
      // Logical space, like everything else. Line width is divided by
      // the scale so the outline stays one screen pixel at any zoom.
      const px = 1 / (view.zoom * dpr);
      ctx.save();
      ctx.setLineDash([4 * px, 3 * px]);
      ctx.strokeStyle = "#2563eb";
      ctx.lineWidth = px;
      ctx.strokeRect(
        Math.min(marquee.from.x, marquee.to.x),
        Math.min(marquee.from.y, marquee.to.y),
        Math.abs(marquee.to.x - marquee.from.x),
        Math.abs(marquee.to.y - marquee.from.y),
      );
      ctx.restore();
    }

    // The in-progress stroke is not in the document yet, so paint it on
    // top rather than committing a partial object every sample.
    // The eraser ring. Without it the tool is invisible: its stroke is
    // white on a white canvas, so there is nothing to show where it is
    // or how much it will take. Two rings — dark over light — so the
    // outline reads on both blank paper and dark ink.
    if (tool === "eraser" && cursorRef.current) {
      const r = (width * ERASER_SCALE) / 2;
      const px = 1 / (view.zoom * dpr);
      ctx.save();
      ctx.beginPath();
      ctx.arc(cursorRef.current.x, cursorRef.current.y, r, 0, Math.PI * 2);
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 3 * px;
      ctx.stroke();
      ctx.strokeStyle = "#111827";
      ctx.lineWidth = px;
      ctx.stroke();
      ctx.restore();
    }

    const live = strokeRef.current?.points;
    if (live && live.length >= 4) {
      // Logical coordinates under the transform renderDoc left set, so
      // the preview sits exactly where the committed stroke will.
      ctx.beginPath();
      ctx.moveTo(live[0], live[1]);
      for (let i = 2; i + 1 < live.length; i += 2) {
        ctx.lineTo(live[i], live[i + 1]);
      }
      ctx.strokeStyle = tool === "eraser" ? "#ffffff" : color;
      ctx.lineWidth = tool === "eraser" ? width * ERASER_SCALE : width;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.stroke();
    }
  }, [doc, view, size, services, color, width, marquee, tool, gridRect, liveMarks]);

  repaintRef.current = paint;

  // Repaint only when something changed. A permanent rAF loop would burn
  // a laptop battery on a board nobody is touching.
  useEffect(() => {
    const id = requestAnimationFrame(paint);
    return () => cancelAnimationFrame(id);
  }, [paint]);

  // ------------------------------------------------------------------
  // Pointer input
  // ------------------------------------------------------------------

  const noteDirty = useCallback((pt: { x: number; y: number }) => {
    hotspotsRef.current.push(pt);
    const d = dirtyRef.current;
    dirtyRef.current = d
      ? {
          x: Math.min(d.x, pt.x),
          y: Math.min(d.y, pt.y),
          w: Math.max(d.x + d.w, pt.x) - Math.min(d.x, pt.x),
          h: Math.max(d.y + d.h, pt.y) - Math.min(d.y, pt.y),
        }
      : { x: pt.x, y: pt.y, w: 1, h: 1 };
  }, []);

  const localPoint = useCallback((e: React.PointerEvent) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    return { x: e.clientX - (rect?.left ?? 0), y: e.clientY - (rect?.top ?? 0) };
  }, []);

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      if (disabled) return;
      const screen = localPoint(e);
      const logical = screenToLogical(screen, view);

      // Checked before any tool: the middle button pans whatever is in
      // hand, so moving the board mid-drawing costs nothing. Putting it
      // after the tool branches would let `text` open an editor and
      // `pen` start a stroke on a middle-click first.
      if (e.button === MIDDLE_BUTTON) {
        // Suppresses the browser's middle-click autoscroll, which would
        // otherwise take over the drag with its own scrolling puck.
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        panFrom.current = screen;
        return;
      }

      if (tool === "text") {
        // No pointer capture and no default action. The browser focuses
        // the canvas on the click that follows this pointerdown, which
        // blurs the textarea we are about to mount — and blur commits,
        // so the editor opened and closed within one gesture.
        e.preventDefault();
        setEditor(logical);
        setEditorText("");
        return;
      }

      // Every other tool drags, so it wants the pointer for the whole
      // gesture even if it leaves the canvas.
      e.currentTarget.setPointerCapture(e.pointerId);

      if (tool === "pan") {
        panFrom.current = screen;
        return;
      }

      if (tool === "select") {
        // A corner handle on the existing selection starts a resize;
        // check it before hit-testing, or grabbing a handle that sits
        // over another object selects that object instead.
        const selBounds = selectionBounds(doc, services);
        const grabbed = selBounds
          ? handleAt(selBounds, logical, view.zoom)
          : null;
        if (selBounds && grabbed) {
          gestureBaseline.current = doc;
          resize.current = {
            anchor: oppositeCorner(selBounds, grabbed),
            start: logical,
          };
          return;
        }

        const hit = hitTest(doc, logical, services);
        if (hit?.selected) {
          // Already selected: this is the start of a move, not a
          // re-select, so leave the selection alone.
          gestureBaseline.current = doc;
          dragFrom.current = logical;
          return;
        }
        commit((prev) => ({
          ...prev,
          objects: prev.objects.map((o) => ({
            ...o,
            selected: hit ? o.id === hit.id : false,
          })),
        }));
        if (hit) {
          gestureBaseline.current = doc;
          dragFrom.current = logical;
        } else {
          // Empty canvas: start a marquee rather than doing nothing.
          setMarquee({ from: logical, to: logical });
        }
        return;
      }
      // pen and eraser both lay down a stroke; the eraser's is composited
      // out at paint time via its own object kind.
      const builder = new StrokeBuilder(
        tool === "eraser" ? "#ffffff" : color,
        tool === "eraser" ? width * ERASER_SCALE : width,
      );
      builder.begin(logical);
      strokeRef.current = builder;
      noteDirty(logical);
    },
    [disabled, localPoint, view, tool, doc, services, commit, color, width, noteDirty],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      if (tool === "eraser") {
        // Follow the pointer even when no button is down: the ring has
        // to show what the eraser would take before it takes it.
        cursorRef.current = screenToLogical(localPoint(e), view);
        if (!strokeRef.current) paint();
      }

      if (panFrom.current) {
        // The delta is computed here, not inside the updater. React runs
        // a state updater at render time — and twice under StrictMode —
        // by which point this ref has been reassigned to `screen` (delta
        // zero, so the board never moves) or nulled by pointerup (a null
        // deref). An updater must close over values, never over a ref it
        // is about to mutate.
        const screen = localPoint(e);
        const from = panFrom.current;
        panFrom.current = screen;
        setView((v) =>
          panBy(v, { x: screen.x - from.x, y: screen.y - from.y }),
        );
        return;
      }
      if (marquee) {
        // Same rule: resolve the pointer position now. Reading the event
        // inside the updater defers it to render time, when the event
        // may no longer be current.
        const to = screenToLogical(localPoint(e), view);
        setMarquee((m) => (m ? { ...m, to } : m));
        return;
      }

      if (resize.current) {
        const { anchor, start } = resize.current;
        const now = screenToLogical(localPoint(e), view);
        const spanX = start.x - anchor.x;
        const spanY = start.y - anchor.y;
        // Degenerate spans would divide by zero; leave that axis alone.
        const sx = Math.abs(spanX) < 1e-6 ? 1 : (now.x - anchor.x) / spanX;
        const sy = Math.abs(spanY) < 1e-6 ? 1 : (now.y - anchor.y) / spanY;
        setDoc((d) => mapSelected(d, (o) => scaleObject(o, sx, sy, anchor)));
        resize.current = { anchor, start: now };
        return;
      }

      if (dragFrom.current) {
        const now = screenToLogical(localPoint(e), view);
        const dx = now.x - dragFrom.current.x;
        const dy = now.y - dragFrom.current.y;
        setDoc((d) => mapSelected(d, (o) => translateObject(o, dx, dy)));
        dragFrom.current = now;
        return;
      }

      const builder = strokeRef.current;
      if (!builder) return;
      const rect = canvasRef.current?.getBoundingClientRect();
      for (const p of strokePointsFromEvent(e.nativeEvent)) {
        const logical = screenToLogical(
          { x: p.x - (rect?.left ?? 0), y: p.y - (rect?.top ?? 0) },
          view,
        );
        builder.extend(logical);
        noteDirty(logical);
      }
      paint();
    },
    [localPoint, size, view, noteDirty, paint, marquee, tool],
  );

  const onPointerUp = useCallback(() => {
    panFrom.current = null;

    if (marquee) {
      const rect = rectFromCorners(marquee.from, marquee.to);
      setMarquee(null);
      // A click with no drag is a deselect, not a zero-area marquee.
      const ids =
        rect.w < 2 && rect.h < 2
          ? []
          : objectsInRect(doc, rect, services);
      commit((prev) => ({
        ...prev,
        objects: prev.objects.map((o) => ({
          ...o,
          selected: ids.includes(o.id),
        })),
      }));
      return;
    }
    // A gesture that edited the selection has already mutated the doc;
    // snapshot it now so undo steps back over the whole drag rather
    // than one pointer sample.
    if (dragFrom.current || resize.current) {
      dragFrom.current = null;
      resize.current = null;
      const baseline = gestureBaseline.current;
      gestureBaseline.current = null;
      // One gesture is one undo step: the drag already mutated the doc
      // live, so all that is left is to record where it started.
      if (baseline) pushHistory(baseline);
      return;
    }
    const builder = strokeRef.current;
    strokeRef.current = null;
    if (!builder) return;
    const stroke = builder.finish();
    if (!stroke) return;
    const object: WbObject =
      tool === "eraser"
        ? ({
            ...stroke,
            kind: "erase",
            mode: "path",
            points: chunkPairs((stroke as { pts: number[] }).pts),
            size: width * ERASER_SCALE,
          } as unknown as WbObject)
        : stroke;
    commit((prev) => ({ ...prev, objects: [...prev.objects, object] }));
  }, [tool, width, commit, pushHistory, marquee, doc, services]);

  const onWheel = useCallback(
    (e: React.WheelEvent<HTMLCanvasElement>) => {
      const screen = {
        x: e.clientX - (canvasRef.current?.getBoundingClientRect().left ?? 0),
        y: e.clientY - (canvasRef.current?.getBoundingClientRect().top ?? 0),
      };
      setView((v) => zoomAt(v, screen, zoomFactorFromWheel(e.deltaY)));
    },
    [],
  );

  /** Commit the open text editor onto the board. */
  const commitEditor = useCallback(() => {
    const at = editor;
    const body = editorText.trim();
    setEditor(null);
    setEditorText("");
    if (!at || !body) return;
    // Recorded verbatim so the model reads the exact characters rather
    // than transcribing its own rendering of them out of the atlas.
    typedRef.current.push(body);
    noteDirty(at);
    commit((prev) => ({
      ...prev,
      objects: [
        ...prev.objects,
        makeTextObject(at, body, TEXT_FONT_SIZE, TEXT_MAX_WIDTH),
      ],
    }));
  }, [editor, editorText, commit, noteDirty]);

  const fitToContent = useCallback(() => {
    setView(zoomToFit(contentBounds(doc, services), size));
  }, [doc, services, size]);

  // ------------------------------------------------------------------
  // Keyboard
  // ------------------------------------------------------------------

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA)$/.test(target.tagName)) return;
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
      } else if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        deleteSelected();
      }
    };
    globalThis.addEventListener("keydown", onKey);
    return () => globalThis.removeEventListener("keydown", onKey);
  }, [undo, redo, deleteSelected]);

  // ------------------------------------------------------------------
  // Ask
  // ------------------------------------------------------------------

  const ask = useCallback(
    async (mode: "sketch" | "deep") => {
      if (!canvasRef.current) return;
      setAskMode(mode);
      const latest = dirtyRef.current;
      const plan = planAtlas(doc, latest, view, size, services);
      const unit = inkHeight(doc, services);
      // Labelled marks: the same ids on the picture, in the note and in
      // the tool, so "right of A3" is one name everywhere. Read off the
      // live document, which is the only record of what the user moved,
      // resized or deleted -- none of which reaches the transcript.
      const marks = boardMarks(doc, services, {
        unit: unit ?? 40,
        newLocalIds: new Set(
          doc.objects
            .filter(
              (o) => o.origin === "local" && !seenAtLastAsk.current.has(o.id),
            )
            .map((o) => o.id),
        ),
      });
      const atlas = buildAtlas(doc, plan, services, marks);
      const hotspots = mapHotspots(plan.sourceRect, hotspotsRef.current);
      const extras: AtlasExtras = {
        mode,
        inkHeight: unit,
        occupied: occupancyCells(doc, plan.sourceRect, services),
        beyond: contentBeyond(doc, plan.sourceRect, services),
        marks,
      };
      seenAtLastAsk.current = new Set(doc.objects.map((o) => o.id));
      if (typedRef.current.length > 0) {
        extras.typedInput = typedRef.current.join("\n\n");
      }
      const selected = doc.objects.find((o) => o.selected);
      if (selected) {
        const b = objectBounds(selected, services);
        if (b) extras.selection = b;
      }

      // Clear the attention accumulators before awaiting: whatever the
      // user draws while the agent is thinking belongs to the next turn.
      hotspotsRef.current = [];
      dirtyRef.current = null;
      typedRef.current = [];
      const text = question;
      setQuestion("");

      await runtime.send(
        text,
        [{ data: atlas.toDataURL("image/png"), mimeType: "image/png" }],
        undefined,
        { whiteboard: atlasMetadata(plan, latest, hotspots, extras) },
      );
    },
    [doc, view, size, services, question, runtime],
  );

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  const artifacts = doc.objects.filter(
    (o): o is Extract<WbObject, { kind: "artifact" }> => o.kind === "artifact",
  );

  // Artifact name/kind/version live on the artifact.created system
  // message, not on the placement command — the model places by id and
  // the metadata arrives on its own event.
  const artifactMeta = useMemo(() => {
    const byId = new Map<
      string,
      { name: string; kind: AgentChatArtifactKind; version: number }
    >();
    for (const m of runtime.messages) {
      const meta = m.systemMeta as
        | {
            artifact_id?: string;
            name?: string;
            kind?: AgentChatArtifactKind;
            version?: number;
          }
        | undefined;
      if (m.systemKind !== "artifact" || !meta?.artifact_id) continue;
      byId.set(meta.artifact_id, {
        name: meta.name ?? "Artifact",
        kind: (meta.kind ?? "markdown") as AgentChatArtifactKind,
        version: Number(meta.version ?? 1),
      });
    }
    return byId;
  }, [runtime.messages]);

  // Asking against a board that is still loading would send an atlas of
  // the half-restored document -- the agent's objects without the ink
  // they answer.
  const busy = runtime.isRunning || disabled || !canvasReady;

  // The newest thing the agent has said it is doing. A sketch turn is a
  // single round-trip and never produces one; a deep turn produces one
  // per iteration, and without them a minute of real work is
  // indistinguishable from a hang.
  const progress = useMemo(() => {
    for (let i = runtime.messages.length - 1; i >= 0; i--) {
      const summary = runtime.messages[i].iterationSummary?.summary?.trim();
      if (summary) return summary;
    }
    return null;
  }, [runtime.messages]);

  return (
    <div className="flex h-full w-full flex-col">
      <div className="relative flex min-h-0 flex-1">
        <div className="absolute left-2 top-2 z-10">
          <ToolRail
            tool={tool}
            onToolChange={setTool}
            color={color}
            onColorChange={setColor}
            width={width}
            onWidthChange={setWidth}
            canUndo={undoStack.current.length > 0}
            canRedo={redoStack.current.length > 0}
            onUndo={undo}
            onRedo={redo}
            onFit={fitToContent}
            disabled={disabled}
            key={historyTick}
          />
        </div>

        <div ref={wrapRef} className="relative min-h-0 flex-1 overflow-hidden">
          <canvas
            ref={canvasRef}
            aria-label="Whiteboard canvas"
            className={cn(
              "h-full w-full touch-none bg-white",
              tool === "pan" ? "cursor-grab" : "cursor-crosshair",
              // Held from the first frame, not from when the veil
              // appears: the load resolves by replacing the document
              // wholesale, so anything drawn before it lands is thrown
              // away.  Better to decline the stroke than to eat it.
              //
              // Held again while the agent works. The atlas and the
              // occupied cells it is answering were captured when Ask
              // was pressed, so ink added now is invisible to it — it
              // places its answer against a board that no longer
              // matches, and can land on top of what was just drawn.
              (!canvasReady || runtime.isRunning) && "pointer-events-none",
            )}
            style={{ width: size.w, height: size.h }}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onPointerLeave={() => {
              // Otherwise the ring stays frozen wherever the pointer
              // left the canvas.
              if (cursorRef.current) {
                cursorRef.current = null;
                paint();
              }
            }}
            onWheel={onWheel}
          />

          {/* The board is held while the agent works, and says so. The
              atlas it is answering was captured at Ask, so a stroke
              added now is invisible to it -- and blocking without
              showing it is worse than not blocking, because the ink
              simply would not appear.

              The label sits at the bottom edge, next to the controls
              that started the turn, rather than over the middle: unlike
              the restoring veil there is real content underneath that
              the user is waiting to see answered. A sketch turn shows
              only that it is thinking; a deep turn reports each step,
              which is the difference between a slow answer and an
              apparent hang. */}
          {runtime.isRunning ? (
            <div className="pointer-events-none absolute inset-0 bg-black/10">
              <div className="absolute inset-x-0 bottom-2 flex justify-center px-12">
                <span className="flex max-w-full items-center gap-2 rounded-full border bg-background px-4 py-1.5 text-sm text-muted-foreground shadow-sm">
                  <Spinner className="size-4 shrink-0" />
                  <span className="truncate">
                    {progress ??
                      (askMode === "deep" ? "Thinking harder…" : "Thinking…")}
                  </span>
                </span>
              </div>
            </div>
          ) : null}

          {/* The veil covers a blank canvas, not a half-drawn one: the
              fold is held until the load lands, so there is nothing
              underneath to show through. It reads as the surface being
              busy, which is what a loading state is for. Tinted, not
              blurred -- there is nothing to blur. */}
          {restoringVisible ? (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-black/10">
              <span className="flex items-center gap-3 rounded-full border bg-background px-6 py-3 text-sm font-medium text-muted-foreground shadow-md">
                <Spinner className="size-5" />
                Restoring board…
              </span>
            </div>
          ) : null}

          {/* A canvas cannot host an iframe, so artifacts render as
              positioned DOM above it. The canvas carries a frame in
              their place, which is what reaches the atlas. */}
          {artifacts.map((a) => {
            const tl = logicalToScreen({ x: a.x, y: a.y }, view);
            const meta = artifactMeta.get(a.artifactId);
            return (
              <div
                key={a.id}
                data-artifact-id={a.artifactId}
                className="pointer-events-auto absolute overflow-auto rounded border bg-background"
                style={{
                  left: tl.x,
                  top: tl.y,
                  width: a.w * view.zoom,
                  height: a.h * view.zoom,
                }}
              >
                {meta && sessionId ? (
                  <ArtifactBlock
                    sessionId={sessionId}
                    artifactId={a.artifactId}
                    name={meta.name}
                    kind={meta.kind}
                    version={a.version ?? meta.version}
                  />
                ) : null}
              </div>
            );
          })}

          {/* The text editor is a DOM overlay rather than canvas text:
              it needs a real caret, IME and selection, none of which a
              canvas provides. */}
          {editor ? (
            <textarea
              autoFocus
              aria-label="Text"
              className="absolute rounded border bg-background p-1 text-sm shadow"
              style={{
                left: logicalToScreen(editor, view).x,
                top: logicalToScreen(editor, view).y,
                width: TEXT_MAX_WIDTH * view.zoom,
              }}
              value={editorText}
              onChange={(e) => setEditorText(e.target.value)}
              onBlur={commitEditor}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.preventDefault();
                  setEditor(null);
                  setEditorText("");
                } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  commitEditor();
                }
              }}
            />
          ) : null}
        </div>
      </div>

      <div className="flex items-center gap-2 border-t p-2">
        {/* Bounded rather than `flex-1`: filling the row pushed the
            buttons onto the right edge, under the host's floating
            Copilot button. Everything now packs left and the corner
            stays clear. `min-w-0` keeps it shrinking on a narrow
            window instead of forcing the buttons off the end. */}
        <input
          className="w-64 min-w-0 shrink rounded border bg-background px-2 py-1 text-sm"
          placeholder="Ask about the board (optional)"
          value={question}
          disabled={busy}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void ask("sketch");
            }
          }}
        />
        <Button
          type="button"
          disabled={busy}
          aria-label="Ask"
          onClick={() => void ask("sketch")}
        >
          <Send className="size-4" />
          Ask
        </Button>
        <Button
          type="button"
          variant="outline"
          disabled={busy}
          aria-label="Think harder"
          onClick={() => void ask("deep")}
        >
          <Brain className="size-4" />
          Think harder
        </Button>
        {onNewBoard ? (
          <Button
            type="button"
            variant="ghost"
            aria-label="New board"
            disabled={busy}
            onClick={() => {
              setWantFresh(true);
              onNewBoard();
            }}
          >
            New board
          </Button>
        ) : null}
        {viewMode && onViewModeChange && hasAnswer ? (
          <div className="flex items-center rounded-full border p-0.5">
            {(["simple", "expert", "whiteboard"] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                aria-label={`${VIEW_MODE_LABELS[mode]} view`}
                aria-pressed={viewMode === mode}
                className={cn(
                  "rounded-full px-3 py-1 text-xs",
                  viewMode === mode
                    ? "bg-background font-medium shadow-sm"
                    : "text-muted-foreground",
                )}
                onClick={() => onViewModeChange(mode)}
              >
                {VIEW_MODE_LABELS[mode]}
              </button>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** Flat [x,y,x,y,...] -> [[x,y],[x,y],...] for the erase path shape. */
function chunkPairs(flat: number[]): number[][] {
  const pairs: number[][] = [];
  for (let i = 0; i + 1 < flat.length; i += 2) pairs.push([flat[i], flat[i + 1]]);
  return pairs;
}

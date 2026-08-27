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
import type { AgentChatAdapter } from "../../types";
import { Button } from "../ui/button";
import {
  type AtlasExtras,
  type Rect,
  atlasMetadata,
  buildAtlas,
  contentBounds,
  mapHotspots,
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
import {
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

export interface AgentWhiteboardProps {
  adapter: AgentChatAdapter;
  agentId?: string;
  sessionId: string | null;
  onSessionChange?: (sessionId: string) => void;
  disabled?: boolean;
}

export function AgentWhiteboard({
  adapter,
  agentId,
  sessionId,
  onSessionChange,
  disabled,
}: AgentWhiteboardProps) {
  const runtime = useAgentChatRuntime({
    adapter,
    agentId,
    sessionId,
    onSessionChange,
  });

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
  const [showTranscript, setShowTranscript] = useState(false);

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

  // ------------------------------------------------------------------
  // Document lifecycle
  // ------------------------------------------------------------------

  // The session this board is currently showing. Compared against the
  // incoming prop so adopting a just-created session is distinguishable
  // from switching to a different one.
  const loadedSession = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    const previous = loadedSession.current;
    loadedSession.current = sessionId;

    if (!shouldReloadCanvas(previous, sessionId)) {
      // Clearing only on a real change: `null -> id` is this board
      // adopting the session its own first Ask created, and everything
      // drawn before that question is still the live document.
      if (!sessionId && previous) setDoc(emptyDoc());
      return;
    }

    let cancelled = false;
    void loadDoc(adapter, sessionId as string, runtime.messages).then(
      (loaded) => {
        if (!cancelled) setDoc(loaded);
      },
    );
    return () => {
      cancelled = true;
    };
    // Deliberately not depending on runtime.messages: this is the load
    // for a session change, and the fold effect below keeps it current.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adapter, sessionId]);

  useEffect(() => {
    setDoc((d) => foldToolCalls(d, runtime.messages));
  }, [runtime.messages]);

  useDebouncedSave(adapter, sessionId, doc);

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
  }, [doc, view, size, services, color, width, marquee, tool]);

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
      const latest = dirtyRef.current;
      const plan = planAtlas(doc, latest, view, size, services);
      const atlas = buildAtlas(doc, plan, services);
      const hotspots = mapHotspots(plan.sourceRect, hotspotsRef.current);
      const extras: AtlasExtras = { mode };
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

  const busy = runtime.isRunning || disabled;

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
        <input
          className="min-w-0 flex-1 rounded border bg-background px-2 py-1 text-sm"
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
        <Button
          type="button"
          variant="ghost"
          aria-label="Toggle transcript"
          aria-expanded={showTranscript}
          onClick={() => setShowTranscript((v) => !v)}
        >
          Transcript
        </Button>
      </div>

      {showTranscript && (
        <div className="max-h-64 overflow-auto border-t p-2 text-sm">
          {runtime.messages.map((m) => (
            <div key={m.id} className="py-1">
              <span className="mr-2 text-xs uppercase text-muted-foreground">
                {m.role}
              </span>
              <span className="whitespace-pre-wrap">{m.content}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Flat [x,y,x,y,...] -> [[x,y],[x,y],...] for the erase path shape. */
function chunkPairs(flat: number[]): number[][] {
  const pairs: number[][] = [];
  for (let i = 0; i + 1 < flat.length; i += 2) pairs.push([flat[i], flat[i + 1]]);
  return pairs;
}

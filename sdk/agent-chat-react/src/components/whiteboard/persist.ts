/**
 * Canvas persistence.
 *
 * The client is the sole writer of the canvas document. The agent never
 * touches the file: its output is a `whiteboard_draw` call in the event
 * log, which this module folds in and includes in the next save. One
 * writer means no race and no sync protocol.
 *
 * The saved document carries `lastEventId`, so a load replays any draw
 * calls newer than the file. That is the recovery tail for a tab closed
 * between an agent reply and the next debounced save — the event log is
 * a backstop, not a second source of truth.
 */
import { useEffect, useRef } from "react";
import type { AgentChatAdapter, AgentChatMessage } from "../../types";
import { type WbDoc, emptyDoc, foldToolCalls } from "./doc";

/**
 * The `_` prefix keeps this out of the workspace file browser (see
 * `_HIDDEN_PREFIXES` in `surogates/api/routes/workspace.py`) so nobody
 * deletes their own board by tidying up.
 */
export const CANVAS_DIR = "_whiteboard";
export const CANVAS_FILE = "canvas.json";
export const CANVAS_PATH = `${CANVAS_DIR}/${CANVAS_FILE}`;

/** Debounce for autosave. Long enough to coalesce a burst of strokes. */
export const SAVE_DEBOUNCE_MS = 1500;

function decode(content: string, encoding: string): string {
  if (encoding !== "base64") return content;
  try {
    return atob(content);
  } catch {
    return "";
  }
}

/**
 * Load the canvas, then replay any newer agent draw calls onto it.
 *
 * Never throws: a missing file, an unreadable one, malformed JSON or an
 * unknown version all start from an empty board rather than failing the
 * whole surface. A corrupt file costs the user their ink, which is bad,
 * but a canvas that refuses to open costs them the feature.
 */
export async function loadDoc(
  adapter: AgentChatAdapter,
  sessionId: string,
  messages: AgentChatMessage[],
): Promise<WbDoc> {
  let doc = emptyDoc();
  try {
    const file = await adapter.getWorkspaceFile({
      sessionId,
      path: CANVAS_PATH,
    });
    const parsed = JSON.parse(decode(file.content, file.encoding)) as WbDoc;
    if (
      parsed &&
      parsed.version === 1 &&
      Array.isArray(parsed.objects)
    ) {
      doc = {
        version: 1,
        objects: parsed.objects,
        lastEventId: Number(parsed.lastEventId) || 0,
      };
    }
  } catch {
    // No saved canvas yet, or it is unreadable — start clean and let the
    // event tail below rebuild whatever the agent has drawn.
  }
  return foldToolCalls(doc, messages);
}

/**
 * Write the canvas document.
 *
 * Best-effort: a failed save is swallowed because the event log still
 * carries the agent's objects, and surfacing an error here would put a
 * network hiccup in front of someone who is drawing.
 */
export async function saveDoc(
  adapter: AgentChatAdapter,
  sessionId: string,
  doc: WbDoc,
): Promise<void> {
  try {
    const body = JSON.stringify(doc);
    const file = new File([body], CANVAS_FILE, { type: "application/json" });
    await adapter.uploadWorkspaceFile({
      sessionId,
      file,
      directory: CANVAS_DIR,
    });
  } catch {
    // Intentionally silent — see the docstring.
  }
}

/**
 * Autosave *doc* on a debounce, and flush once on unmount.
 *
 * The flush matters: without it, closing the tab within the debounce
 * window drops the last strokes, and those are the ones the user just
 * made.
 */
export function useDebouncedSave(
  adapter: AgentChatAdapter,
  sessionId: string | null,
  doc: WbDoc,
  delayMs: number = SAVE_DEBOUNCE_MS,
): void {
  // Held in a ref so the unmount effect can flush the newest document
  // without re-subscribing on every keystroke.
  const latest = useRef(doc);
  latest.current = doc;
  const dirty = useRef(false);

  useEffect(() => {
    if (!sessionId) return;
    // The first render carries the freshly-loaded document; saving it
    // straight back would be a pointless round-trip on every open.
    if (!dirty.current) {
      dirty.current = true;
      return;
    }
    const timer = setTimeout(() => {
      void saveDoc(adapter, sessionId, latest.current);
    }, delayMs);
    return () => clearTimeout(timer);
  }, [adapter, sessionId, doc, delayMs]);

  useEffect(() => {
    if (!sessionId) return;
    return () => {
      void saveDoc(adapter, sessionId, latest.current);
    };
  }, [adapter, sessionId]);
}

// What the agent tells this computer's user while Surogate's window is away (spec, Section 8),
// read in the main process on the app's own sign-in: each new item of their inbox, which is a
// question, an approval, something to do, a check-in or a chat that finished, and the end of
// each turn of the chat the window shows, which the agent leaves out of the inbox while a page
// streams that chat. Each stream reconnects whenever it closes, with the device link's backoff,
// and one silent for longer than the agent's pings leave between them is taken as dropped.

import { setTimeout as sleep } from "node:timers/promises";

import { reconnectDelayMs } from "../link/backoff.js";
import { readEvents, type ServerEvent } from "./sse.js";

/** *path* on the agent, as the signed-in user: DesktopSession.api. */
export type Api = (path: string, init?: RequestInit) => Promise<Response>;

// sse-starlette pings every 15 s: a stream silent this long has dropped.
export const SILENCE_MS = 45_000;
// A stream held this long did its work: one that closes after it reconnects at once, one that closes sooner backs off.
const HELD_MS = 30_000;

export interface FollowOptions {
  api: Api;
  agentId: string; // named on each call: an agent served on no subdomain of its own knows itself by it
  onError(error: unknown): void;
  delayMs?: (attempt: number) => number; // the wait before reconnect attempt *attempt*, 0 first
  silenceMs?: number;
}

const parsed = (data: string): Record<string, unknown> | null => {
  try {
    const value: unknown = JSON.parse(data);
    return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
};

/** Follow the stream at *path()* until the returned stop is called: *onEvent* hears each event, through every reconnection. */
export function follow(options: FollowOptions, path: () => string, onEvent: (event: ServerEvent) => void): () => void {
  const stopped = new AbortController();
  void (async () => {
    let attempt = 0;
    while (!stopped.signal.aborted) {
      const dropped = new AbortController();
      let silence: NodeJS.Timeout | undefined;
      let opened = 0;
      const heard = () => {
        clearTimeout(silence);
        silence = setTimeout(() => dropped.abort(), options.silenceMs ?? SILENCE_MS);
      };
      try {
        heard();
        const asked = path();
        const response = await options.api(asked, { signal: AbortSignal.any([stopped.signal, dropped.signal]) });
        if (!response.ok || !response.body) throw new Error(`The agent did not open ${asked} (HTTP ${response.status})`);
        opened = Date.now();
        await readEvents(response.body, onEvent, heard);
      } catch (error) {
        // A stop ends it; a silence is a drop, said by nothing more than the reconnection.
        if (stopped.signal.aborted) return;
        if (!dropped.signal.aborted) options.onError(error);
      } finally {
        clearTimeout(silence);
        if (opened !== 0 && Date.now() - opened >= HELD_MS) attempt = 0;
      }
      await sleep((options.delayMs ?? reconnectDelayMs)(attempt++), undefined, { signal: stopped.signal }).catch(() => {});
    }
  })();
  return () => stopped.abort();
}

export interface InboxItem {
  id: number;
  kind: string; // input_required, action_required, governance_gate, task_complete or progress_checkin
  title: string; // the agent's words: a question, or a chat's title
  sessionId: string;
}

/**
 * Follow the user's inbox: *onItem* hears each item that comes while it waits on them, once. What
 * the inbox held when it was first opened is not told; what came while it was out of reach is,
 * once it is back.
 */
export function followInbox(options: FollowOptions & { onItem(item: InboxItem): void }): () => void {
  const known = new Set<number>();
  let opened = false;
  // An item whose read fails, as an agent that restarts answers, is read again when a later snapshot lists it;
  // one the agent does not have, or answered meanwhile, is not.
  const tell = async (id: number): Promise<void> => {
    if (known.has(id)) return;
    known.add(id);
    try {
      const response = await options.api(`/api/v1/inbox/${id}?agent_id=${encodeURIComponent(options.agentId)}`);
      if (response.status >= 500 || response.status === 429) known.delete(id);
      if (!response.ok) return;
      const { status, kind, title, session_id: sessionId } = parsed(await response.text()) ?? {};
      if (status !== "pending" || typeof kind !== "string" || typeof title !== "string" || typeof sessionId !== "string") return;
      options.onItem({ id, kind, title, sessionId });
    } catch (error) {
      known.delete(id);
      options.onError(error);
    }
  };
  return follow(options, () => `/api/v1/inbox/stream?agent_id=${encodeURIComponent(options.agentId)}`, (event) => {
    const data = parsed(event.data);
    if (event.type === "snapshot" && Array.isArray(data?.unread_ids)) {
      const ids = data.unread_ids.filter((id): id is number => Number.isInteger(id));
      for (const id of ids) {
        if (opened) void tell(id);
        else known.add(id);
      }
      opened = true;
    } else if (event.type === "item" && Number.isInteger(data?.item_id)) {
      void tell(data!.item_id as number);
    }
  });
}

/** Chat *sessionId*'s title, as the agent names it; "A chat" for one it names not. */
export async function titleOf(api: Api, agentId: string, sessionId: string): Promise<string> {
  const response = await api(`/api/v1/sessions/${sessionId}?agent_id=${encodeURIComponent(agentId)}`);
  const { title } = response.ok ? (parsed(await response.text()) ?? {}) : {};
  return typeof title === "string" && title !== "" ? title : "A chat";
}

// What ends a chat's follow: the chat archived, or gone. Any other close is followed by a reconnection.
const GONE: ReadonlySet<unknown> = new Set(["archived", "session_not_found"]);

/**
 * Follow chat *sessionId* across its turns (watch=1): *onTurnEnd* hears the chat's title each time one
 * of its turns ends. It starts at the chat's newest event, and takes up after the last event it heard,
 * stream.start's included, whenever its stream closes. Only a chat archived or gone ends it, short of
 * stop. Until *started*, it has heard nothing of the chat: a turn that ends meanwhile it never tells.
 */
export function followChat(options: FollowOptions & { sessionId: string; onTurnEnd(title: string): void }): { stop(): void; started(): boolean } {
  let after = -1;
  const told = async (): Promise<void> => {
    try {
      options.onTurnEnd(await titleOf(options.api, options.agentId, options.sessionId));
    } catch (error) {
      options.onError(error);
    }
  };
  // A chat the agent does not have as its stream opens, deleted or another organisation's, is gone too.
  const api: Api = async (path, init) => {
    const response = await options.api(path, init);
    if (response.status === 404) stop();
    return response;
  };
  const stop = follow({ ...options, api }, () => `/api/v1/sessions/${options.sessionId}/events?after=${after}&watch=1`, (event) => {
    if (event.id !== null && /^\d+$/.test(event.id)) after = Number(event.id);
    if (event.type === "session.complete") void told();
    else if (event.type === "session.done" && GONE.has(parsed(event.data)?.reason)) stop();
  });
  return { stop, started: () => after >= 0 };
}

// More than this many items close together are told as one notice: a night asleep, or an agent's own burst.
export const BURST = 3;
export const BURST_MS = 5_000;

/** Inbox items told close together: *add* gives the items of the burst an item joins, each within *withinMs* of the one before. */
export class Burst {
  private items: InboxItem[] = [];
  private last = Number.NEGATIVE_INFINITY;

  constructor(private readonly withinMs = BURST_MS, private readonly now: () => number = Date.now) {}

  add(item: InboxItem): readonly InboxItem[] {
    const at = this.now();
    if (at - this.last > this.withinMs) this.items = [];
    this.last = at;
    this.items.push(item);
    return this.items;
  }
}

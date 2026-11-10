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
export function follow(options: Omit<FollowOptions, "agentId">, path: () => string, onEvent: (event: ServerEvent) => void): () => void {
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

// What the agent's route for chat *sessionId* (*under* it, if given) answers; nothing where it cannot be read.
async function chatRead(api: Api, agentId: string, sessionId: string, under = ""): Promise<Record<string, unknown>> {
  const response = await api(`/api/v1/sessions/${sessionId}${under}?agent_id=${encodeURIComponent(agentId)}`);
  return response.ok ? (parsed(await response.text()) ?? {}) : {};
}

/** Chat *sessionId*'s title, as the agent names it; "A chat" for one it names not. */
export async function titleOf(api: Api, agentId: string, sessionId: string): Promise<string> {
  const { title } = await chatRead(api, agentId, sessionId);
  return typeof title === "string" && title !== "" ? title : "A chat";
}

// Whether the agent holds a question of chat *sessionId* to its user open, waiting for the answer.
async function waitsOnItsUser(api: Api, agentId: string, sessionId: string): Promise<boolean> {
  const query = `session_id=${encodeURIComponent(sessionId)}&kind=input_required&status=pending&limit=1`;
  const response = await api(`/api/v1/inbox?${query}&agent_id=${encodeURIComponent(agentId)}`);
  const { items } = response.ok ? (parsed(await response.text()) ?? {}) : {};
  return Array.isArray(items) && items.length > 0;
}

// The server lists at most this many sessions of a chat's tree (_MAX_TREE_NODES): a full list may leave some out.
const TREE_CAP = 200;

/**
 * Whether chat *sessionId* is at rest between its turns, as the agent has it now: true where its last turn is done or
 * failed and no helper under it (which works in its folder too) is under way, false where it or a helper is,
 * undefined where it says neither (paused), cannot be read, or lists too many to be sure it named every helper.
 * A helper stopped, deleted or archived works no more.
 */
export async function restOf(api: Api, agentId: string, sessionId: string, asked = false): Promise<boolean | undefined> {
  // Its stream said it asked its user something: believed only while the agent still holds that question open. A
  // question nobody answered in time is closed there with no event on the stream, and the turn goes on.
  const [{ nodes }, asking] = await Promise.all([
    chatRead(api, agentId, sessionId, "/tree"),
    asked ? waitsOnItsUser(api, agentId, sessionId) : false,
  ]);
  if (!Array.isArray(nodes)) return undefined;
  const sessions = nodes as Array<{ id?: unknown; status?: unknown }>;
  const own = sessions.find((node) => node?.id === sessionId)?.status;
  // *asking*: the chat waits on its user's answer, which the agent counts as active; only a helper at work then is.
  const helping = sessions.some((node) => node?.id !== sessionId && node?.status === "active");
  if (helping || (own === "active" && !asking)) return false;
  const done = own === "completed" || own === "failed" || (asking && own === "active");
  return sessions.length < TREE_CAP && done ? true : undefined;
}

// The events that end a chat's turn: done, failed, or stopped from a channel; a Stop's pause only once the turn has
// stopped, undo done (turn_ended), not the pause route's own, which the turn hears of while it may still run.
const TURN_ENDS: ReadonlySet<string> = new Set(["session.complete", "session.fail", "session.stopped"]);
const endsTheTurn = (event: ServerEvent): boolean =>
  TURN_ENDS.has(event.type) || (event.type === "session.pause" && parsed(event.data)?.turn_ended === true);

// The events that start a chat's turn, or take one up again: a message to it, the harness's wake, a resume.
// An answer to the chat's question carries its turn on, as a message does.
const TURN_STARTS: ReadonlySet<string> = new Set([
  "user.message", "coordinator.message", "harness.wake", "session.resume", "ask_user_question.response",
]);
// The chat asked its user something and waits for the answer: nothing of its turn runs meanwhile.
const ASKS = "inbox.input_required";

// What ends a chat's follow: the chat archived, or gone. Any other close is followed by a reconnection.
const GONE: ReadonlySet<unknown> = new Set(["archived", "session_not_found"]);

/**
 * Follow chat *sessionId* across its turns (watch=1): *onTurnEnd* is called each time one of its turns
 * ends, done or not: a turn that failed or was stopped has ended too; *onTurnStart* each time one starts.
 * It starts at the chat's newest event, and takes up after the last event it heard, stream.start's
 * included, whenever its stream closes. Only a chat archived or gone ends it, short of stop. Until *started*, it has heard nothing of the chat: a turn
 * that ends meanwhile it never tells.
 */
export function followTurns(
  options: Omit<FollowOptions, "agentId"> & {
    sessionId: string; onTurnEnd(done: boolean): void; onTurnStart?(): void; onAsking?(): void; live?(): boolean;
  },
): { stop(): void; started(): boolean } {
  let after = -1;
  // Followed only while *live* says so, as the sign-in it was started under: once that has ended it stops, and
  // neither asks the agent nor tells a turn's end again.
  const ended = (): boolean => {
    const over = options.live?.() === false;
    if (over) stop();
    return over;
  };
  // A chat the agent does not have as its stream opens, deleted or another organisation's, is gone too.
  const api: Api = async (path, init) => {
    if (ended()) throw new Error("the sign-in this chat's turns were followed under has ended");
    const response = await options.api(path, init);
    if (response.status === 404) stop();
    return response;
  };
  const stop = follow({ ...options, api }, () => `/api/v1/sessions/${options.sessionId}/events?after=${after}&watch=1`, (event) => {
    if (ended()) return;
    if (event.id !== null && /^\d+$/.test(event.id)) after = Number(event.id);
    if (endsTheTurn(event)) options.onTurnEnd(event.type === "session.complete");
    else if (TURN_STARTS.has(event.type)) options.onTurnStart?.();
    else if (event.type === ASKS) options.onAsking?.();
    else if (event.type === "session.done" && GONE.has(parsed(event.data)?.reason)) stop();
  });
  return { stop, started: () => after >= 0 };
}

/** Chat *sessionId* followed across its turns: *onTurnEnd* hears the chat's title each time one is done. */
export function followChat(options: FollowOptions & { sessionId: string; onTurnEnd(title: string): void }): { stop(): void; started(): boolean } {
  return followTurns({
    ...options,
    onTurnEnd: async (done) => {
      // Told only of a turn done: a failed or stopped one is not this notice's to tell.
      if (!done) return;
      try {
        options.onTurnEnd(await titleOf(options.api, options.agentId, options.sessionId));
      } catch (error) {
        options.onError(error);
      }
    },
  });
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

// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// useMissionEvents — owns the mission-wide event feed. One full
// backfill runs on mount regardless of mission status (terminal
// missions still need Activity/History for post-mortems), paging the
// after_id cursor until a short page. While the mission is live, an
// interval re-pumps incrementally from the last seen event id. Fetch
// failures are non-fatal: the last good feed is kept and the next
// tick retries.
import { useCallback, useEffect, useRef, useState } from "react";

import type {
  AgentChatAdapter,
  AgentChatMissionEvent,
  AgentChatMissionEventSession,
} from "../../types";

import { mergeMissionEvents } from "./mission-derive";

const PAGE_LIMIT = 500;
// Runaway guard for one pump, not a practical bound (10k events).
const MAX_PAGES_PER_PUMP = 20;

export type MissionEventsFeed = {
  /** False when the adapter doesn't implement listMissionEvents —
   * callers hide the event-driven UI sections entirely. */
  supported: boolean;
  /** Ascending by event id. */
  events: AgentChatMissionEvent[];
  sessions: Record<string, AgentChatMissionEventSession>;
};

export function useMissionEvents(input: {
  adapter: AgentChatAdapter;
  missionId: string;
  /** Null until the mission summary has loaded. */
  missionStatus: string | null;
  isTerminal: boolean;
  pollIntervalMs: number;
}): MissionEventsFeed {
  const { adapter, missionId, missionStatus, isTerminal, pollIntervalMs } =
    input;
  const supported = typeof adapter.listMissionEvents === "function";

  const [events, setEvents] = useState<AgentChatMissionEvent[]>([]);
  const [sessions, setSessions] = useState<
    Record<string, AgentChatMissionEventSession>
  >({});
  const lastIdRef = useRef<number | null>(null);
  const pumpingRef = useRef(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Reset the feed when the mission changes.
  useEffect(() => {
    lastIdRef.current = null;
    setEvents([]);
    setSessions({});
  }, [missionId]);

  const pump = useCallback(async () => {
    const fn = adapter.listMissionEvents;
    if (typeof fn !== "function" || pumpingRef.current) return;
    pumpingRef.current = true;
    try {
      for (let page = 0; page < MAX_PAGES_PER_PUMP; page += 1) {
        const resp = await fn.call(adapter, {
          missionId,
          afterId: lastIdRef.current ?? undefined,
          limit: PAGE_LIMIT,
        });
        if (!mountedRef.current) return;
        if (resp.events.length > 0) {
          lastIdRef.current = resp.events[resp.events.length - 1].id;
          setEvents((prev) => mergeMissionEvents(prev, resp.events));
        }
        if (Object.keys(resp.sessions).length > 0) {
          setSessions((prev) => ({ ...prev, ...resp.sessions }));
        }
        if (resp.events.length < PAGE_LIMIT) break;
      }
    } catch {
      // Non-fatal: keep the last good feed; the next tick retries.
    } finally {
      pumpingRef.current = false;
    }
  }, [adapter, missionId]);

  // Initial backfill — runs for terminal missions too.
  useEffect(() => {
    void pump();
  }, [pump]);

  // Incremental polling only while the mission is live.
  useEffect(() => {
    if (!supported || missionStatus === null || isTerminal) return;
    const id = window.setInterval(() => {
      void pump();
    }, pollIntervalMs);
    return () => {
      window.clearInterval(id);
    };
  }, [supported, missionStatus, isTerminal, pollIntervalMs, pump]);

  return { supported, events, sessions };
}

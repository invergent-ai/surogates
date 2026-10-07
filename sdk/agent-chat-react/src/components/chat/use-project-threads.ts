// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A project's thread rows, kept live from its stream (desktop design, Section 12).

import { useEffect, useState } from "react";
import type { AgentChatAdapter, AgentChatThreadRow } from "../../types";

/**
 * The rows of *projectId*'s threads, by id: read whole when the stream says it
 * is ready (again, after a reconnect) or a change is project-wide, and one row
 * when a change names its thread. One read runs at a time; changes heard during
 * it are read after it, and a read that failed is read again with the next
 * change. Empty without a project, or with an adapter that reads none.
 */
export function useProjectThreads(
  adapter: AgentChatAdapter,
  projectId: string | null,
): Record<string, AgentChatThreadRow> {
  const [rows, setRows] = useState<Record<string, AgentChatThreadRow>>({});

  useEffect(() => {
    setRows({});
    if (!projectId || !adapter.listProjectThreads || !adapter.openProjectStream) return;
    let closed = false;
    let reading = false;
    let wanted = new Set<string | null>();
    const failed = new Set<string | null>();

    const readOne = async (threadId: string | null) => {
      if (threadId === null) {
        const all = await adapter.listProjectThreads!({ projectId });
        if (!closed) setRows(Object.fromEntries(all.map((row) => [row.id, row])));
        return;
      }
      const [row] = await adapter.listProjectThreads!({ projectId, threadId });
      if (!closed) setRows(({ [threadId]: _gone, ...rest }) => (row ? { ...rest, [threadId]: row } : rest));
    };
    const read = async () => {
      if (reading) return;
      reading = true;
      while (wanted.size > 0 && !closed) {
        const asked = wanted;
        wanted = new Set();
        // A full read answers every one-row read asked with it.
        for (const threadId of asked.has(null) ? [null] : asked) {
          if (closed) break;
          try {
            await readOne(threadId);
          } catch {
            failed.add(threadId);
          }
        }
      }
      reading = false;
    };
    const want = (threadId: string | null) => {
      wanted.add(threadId);
      for (const again of failed) wanted.add(again);
      failed.clear();
      void read();
    };

    const stream = adapter.openProjectStream({ projectId });
    // The stream opens itself again after a failure, and ends only when the
    // project is gone: its cards then show what their events said, and a read
    // still on its way is dropped.
    stream.onerror = () => {
      if (closed) return;
      closed = true;
      setRows({});
    };
    stream.addEventListener("ready", () => want(null));
    stream.addEventListener("change", (event) => {
      let threadId: string | null = null;
      try {
        const named = (JSON.parse(event.data) as { thread_id?: unknown }).thread_id;
        threadId = typeof named === "string" ? named : null;
      } catch {
        // Unreadable: read the project's rows whole.
      }
      want(threadId);
    });
    return () => {
      closed = true;
      stream.close();
    };
  }, [adapter, projectId]);

  return rows;
}

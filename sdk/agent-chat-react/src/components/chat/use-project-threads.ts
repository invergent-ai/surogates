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
 * it are read after it. Empty without a project, or with an adapter that reads none.
 */
export function useProjectThreads(
  adapter: AgentChatAdapter,
  projectId: string | null,
): Record<string, AgentChatThreadRow> {
  const [rows, setRows] = useState<Record<string, AgentChatThreadRow>>({});

  useEffect(() => {
    setRows({});
    const { listProjectThreads, openProjectStream } = adapter;
    if (!projectId || !listProjectThreads || !openProjectStream) return;
    let closed = false;
    let reading = false;
    let wanted = new Set<string | null>();

    const read = async () => {
      if (reading) return;
      reading = true;
      try {
        while (wanted.size > 0 && !closed) {
          const asked = wanted;
          wanted = new Set();
          if (asked.has(null)) {
            const all = await listProjectThreads({ projectId });
            if (!closed) setRows(Object.fromEntries(all.map((row) => [row.id, row])));
            continue;
          }
          for (const threadId of asked as Set<string>) {
            const [row] = await listProjectThreads({ projectId, threadId });
            if (closed) return;
            setRows(({ [threadId]: _gone, ...rest }) => (row ? { ...rest, [threadId]: row } : rest));
          }
        }
      } catch {
        // The stream's next change, or its next ready, reads again.
      } finally {
        reading = false;
      }
    };
    const want = (threadId: string | null) => {
      wanted.add(threadId);
      void read();
    };

    const stream = openProjectStream({ projectId });
    // The stream opens itself again after a failure, and ends only when the
    // project is gone: its cards then show what their events said.
    stream.onerror = () => {
      if (!closed) setRows({});
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

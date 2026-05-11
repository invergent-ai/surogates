// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { useEffect, useState } from "react";
import type { AgentChatAdapter, AgentChatInboxEventStream } from "../../types";

export interface InboxUnreadCountState {
  unreadCount: number;
  hasLoaded: boolean;
  error: string | null;
}

export function useInboxUnreadCount(
  adapter: AgentChatAdapter,
): InboxUnreadCountState {
  const [unreadCount, setUnreadCount] = useState(0);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let stream: AgentChatInboxEventStream | null = null;

    if (!adapter.listInbox || !adapter.openInboxStream) {
      setError("Inbox is not supported by this adapter.");
      setHasLoaded(true);
      return () => {
        cancelled = true;
      };
    }

    adapter
      .listInbox({ status: "pending", limit: 200 })
      .then((response) => {
        if (cancelled) return;
        setUnreadCount(response.items.filter((item) => !item.readAt).length);
        setHasLoaded(true);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load inbox");
        setHasLoaded(true);
      });

    try {
      stream = adapter.openInboxStream();
      stream.addEventListener("snapshot", (event) => {
        if (cancelled) return;
        const payload = JSON.parse(event.data) as { unread_ids?: unknown };
        const ids = Array.isArray(payload.unread_ids) ? payload.unread_ids : [];
        setUnreadCount(ids.length);
        setHasLoaded(true);
      });
      stream.addEventListener("item", () => {
        if (cancelled) return;
        setUnreadCount((count) => count + 1);
        setHasLoaded(true);
      });
      stream.onerror = () => {
        if (!cancelled) setError("Inbox stream disconnected.");
      };
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to open inbox stream");
    }

    return () => {
      cancelled = true;
      stream?.close();
    };
  }, [adapter]);

  return { unreadCount, hasLoaded, error };
}

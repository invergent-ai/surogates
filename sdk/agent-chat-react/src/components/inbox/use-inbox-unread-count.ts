// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentChatAdapter, AgentChatInboxEventStream } from "../../types";

export interface InboxUnreadCountState {
  unreadCount: number;
  hasLoaded: boolean;
  error: string | null;
}

/**
 * How many pending inbox items the user has not opened.
 *
 * Re-derived from the list on every signal rather than adjusted in
 * place: the stream carries "something changed", not a delta, so
 * counting its nudges only ever pushed the badge up — it never came back
 * down when the user read, answered or dismissed an item.
 */
export function useInboxUnreadCount(
  adapter: AgentChatAdapter,
): InboxUnreadCountState {
  const [unreadCount, setUnreadCount] = useState(0);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);

  const listInbox = adapter.listInbox;
  const openInboxStream = adapter.openInboxStream;

  const refetch = useCallback(async () => {
    if (!listInbox) return;
    const id = ++requestId.current;
    try {
      const response = await listInbox({ status: "pending", limit: 200 });
      if (id !== requestId.current) return;
      setUnreadCount(response.items.filter((item) => !item.readAt).length);
      setError(null);
    } catch (err) {
      if (id !== requestId.current) return;
      // Keep the last known count: a transient failure is not "zero".
      setError(err instanceof Error ? err.message : "Failed to load inbox");
    } finally {
      if (id === requestId.current) setHasLoaded(true);
    }
  }, [listInbox]);

  useEffect(() => {
    if (!listInbox || !openInboxStream) {
      setError("Inbox is not supported by this adapter.");
      setHasLoaded(true);
      return;
    }

    void refetch();
    let stream: AgentChatInboxEventStream | null = null;
    try {
      stream = openInboxStream();
      const onChange = () => void refetch();
      stream.addEventListener("snapshot", onChange);
      stream.addEventListener("item", onChange);
      stream.onerror = () => setError("Inbox stream disconnected.");
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to open inbox stream",
      );
    }

    return () => {
      // Abandons any refetch still in flight, so a late response cannot
      // set state after unmount.
      requestId.current += 1;
      stream?.close();
    };
  }, [listInbox, openInboxStream, refetch]);

  return { unreadCount, hasLoaded, error };
}

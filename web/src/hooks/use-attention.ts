// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// When the agent writes while this tab is in the background, the title
// flashes and the app icon shows a badge, until the person looks again.

import { attentionTitle } from "@/lib/push-offer";
import { useEffect, useRef } from "react";

export function useAttention(lastAssistantId: string | null): void {
  const seen = useRef(lastAssistantId);
  const unread = useRef(0);
  const base = useRef(document.title);

  useEffect(() => {
    if (lastAssistantId === seen.current) {
      return;
    }
    seen.current = lastAssistantId;
    if (lastAssistantId === null || !document.hidden) {
      return;
    }
    unread.current += 1;
    navigator.setAppBadge?.(unread.current).catch(() => undefined);
  }, [lastAssistantId]);

  useEffect(() => {
    let on = false;
    const timer = window.setInterval(() => {
      if (unread.current === 0) {
        return;
      }
      on = !on;
      document.title = attentionTitle(base.current, unread.current, on);
    }, 1200);
    const back = () => {
      if (document.hidden) {
        return;
      }
      unread.current = 0;
      document.title = base.current;
      navigator.clearAppBadge?.().catch(() => undefined);
      void navigator.serviceWorker
        ?.getRegistration("/")
        .then((r) => r?.getNotifications())
        .then((shown) => {
          for (const n of shown ?? []) {
            n.close();
          }
        })
        .catch(() => undefined);
    };
    document.addEventListener("visibilitychange", back);
    back();
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", back);
      document.title = base.current;
    };
  }, []);
}

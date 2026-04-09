// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only

import { useEffect, useRef } from "react";

export function useInfiniteScroll(fetchMore: () => void) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) {
      return;
    }
    const obs = new IntersectionObserver(
      ([e]) => {
        if (e.isIntersecting) {
          fetchMore();
        }
      },
      { threshold: 0, root: scrollRef.current },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [fetchMore]);

  return { scrollRef, sentinelRef };
}
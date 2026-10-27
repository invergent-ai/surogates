// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { cn } from "./lib/utils";
import { type CSSProperties, useEffect, useRef, useState } from "react";

// CSS mask that keeps only the border ring of a box: a gradient set as the
// element's background then fills just the border (the interior is masked out).
// Pair with a `padding` equal to the desired border thickness.
const RING_MASK: CSSProperties = {
  WebkitMask:
    "linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0)",
  WebkitMaskComposite: "xor",
  maskComposite: "exclude",
};

export interface PillTab<T extends string> {
  id: T;
  label: string;
  /** Getting-started spotlight anchor rendered as ``data-onboarding``. */
  onboarding?: string;
}

// The product's horizontal tab bar (Figma "tabBar"): the inner header on every
// detail page that has sub-sections — agent Configure, models, training runs,
// datasets, knowledge bases. Active tab gets a subtle surface fill, foreground
// text and an amber border-glow; inactive tabs are muted.
//
// `tabs` draws the same pills over Radix Tabs (with panels); this one is a
// bare strip for a page that swaps its own content by id.
//
// A dozen tabs never fit a phone, so the strip scrolls horizontally there. Two
// things make that usable: the strip scrolls the active tab into view (a
// deep-linked `?tab=monetize` would otherwise land off-screen with nothing to
// hint it exists), and a fade on the trailing edge advertises the overflow.
//
// Only the strip moves. `scrollIntoView` would scroll every ancestor too, the
// page included, so a page opened on a phone would land below its own header
// and a pick on a desktop would jump the page to the tabs.
export function PillTabs<T extends string>({
  tabs,
  activeId,
  onSelect,
  label = "Sections",
  flush = false,
}: {
  tabs: readonly PillTab<T>[];
  activeId: T;
  onSelect: (id: T) => void;
  /** Names the tablist for screen readers. */
  label?: string;
  /** Drop the horizontal padding.
   *
   * The default gutter is for a full-bleed page, where this strip spans the
   * viewport and supplies its own inset. Inside a container that already pads
   * — a detail view nested in a padded page — that gutter indents the tabs
   * relative to everything above and below them. */
  flush?: boolean;
}) {
  const stripRef = useRef<HTMLDivElement | null>(null);
  const activeRef = useRef<HTMLButtonElement | null>(null);
  // The tab the strip was last placed for; null before the first placement.
  const placedFor = useRef<T | null>(null);
  // Scrolled past the first tab: a matching fade on the leading edge says
  // there is more that way, instead of a word cut in half.
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const strip = stripRef.current;
    const pill = activeRef.current;
    if (placedFor.current === activeId) return;
    const first = placedFor.current === null;
    placedFor.current = activeId;
    if (!strip || !pill) return;

    const stripBox = strip.getBoundingClientRect();
    const pillBox = pill.getBoundingClientRect();
    // The pill's span in the strip's own scrolling coordinates.
    const start = pillBox.left - stripBox.left + strip.scrollLeft;
    const end = start + pillBox.width;
    // A tab chosen on the first paint needs no motion unless it is cut off or
    // out of sight.
    if (first && start >= strip.scrollLeft && end <= strip.scrollLeft + stripBox.width) {
      return;
    }
    strip.scrollTo({
      left: Math.max(0, start - (stripBox.width - pillBox.width) / 2),
      behavior: first ? "instant" : "smooth",
    });
  }, [activeId]);

  return (
    <div data-slot="pill-tabs" className="relative shrink-0 border-b border-line">
      <div
        ref={stripRef}
        role="tablist"
        aria-label={label}
        onScroll={(e) => setScrolled(e.currentTarget.scrollLeft > 4)}
        className={cn(
          "no-scrollbar flex snap-x snap-proximity gap-1.5 overflow-x-auto py-2 md:py-2.5",
          !flush && "px-4 md:px-7",
        )}
      >
        {tabs.map((tab) => {
          const isActive = tab.id === activeId;
          return (
            <button
              key={tab.id}
              ref={isActive ? activeRef : undefined}
              type="button"
              role="tab"
              data-slot="pill-tabs-tab"
              aria-selected={isActive}
              data-onboarding={tab.onboarding}
              onClick={() => onSelect(tab.id)}
              className={cn(
                "relative flex min-h-11 shrink-0 snap-center items-center whitespace-nowrap rounded-[10px] px-3.5 text-sm font-medium transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/40 pointer-fine:min-h-0 md:px-3 md:py-1.5",
                isActive
                  ? "bg-surface text-foreground"
                  : "text-muted-foreground active:bg-surface/60 md:hover:bg-surface/60 md:hover:text-foreground",
              )}
            >
              {/* Amber border-glow: bright along the bottom edge, fading upward
                  along the rounded corners. The blurred copy adds the bloom. */}
              {isActive && (
                <>
                  <span
                    aria-hidden="true"
                    className="pointer-events-none absolute inset-0 rounded-[10px] blur-[2px]"
                    style={{
                      padding: "2px",
                      background:
                        "linear-gradient(to top, rgba(245,158,11,0.75) 10%, rgba(245,158,11,0) 40%)",
                      ...RING_MASK,
                    }}
                  />
                  <span
                    aria-hidden="true"
                    className="pointer-events-none absolute inset-0 rounded-[10px]"
                    style={{
                      padding: "1px",
                      background:
                        "linear-gradient(to top, rgba(245,158,11,1) 10%, rgba(245,158,11,0) 40%)",
                      ...RING_MASK,
                    }}
                  />
                </>
              )}
              <span className="relative">{tab.label}</span>
            </button>
          );
        })}
      </div>
      {/* Overflow hints. Decorative and non-interactive, so taps still reach
          the tab underneath. */}
      {scrolled && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 left-0 w-8 bg-gradient-to-r from-background to-transparent md:hidden"
        />
      )}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-background to-transparent md:hidden"
      />
    </div>
  );
}

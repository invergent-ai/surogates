// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { ArrowLeft } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { useIsMobile } from "./lib/use-is-mobile";
import { cn } from "./lib/utils";
import { MOBILE_PAGE_ACTIONS_ID } from "./mobile-top-bar";

/**
 * The phone top bar's action slot, once it exists. The bar is rendered by the
 * layout above the page, so it is in the DOM by the time any page commits —
 * but not during that page's first render, hence the effect.
 */
function useMobileActionSlot(): HTMLElement | null {
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  // Deliberately no dependency array. Resolving once and caching forever
  // assumes the bar outlives every page that portals into it; if it is ever
  // replaced, the cached node is detached and the portal writes into nothing
  // — the action would simply disappear, with no error. The check is one
  // property read in the common case.
  useEffect(() => {
    setSlot((current) =>
      current?.isConnected
        ? current
        : document.getElementById(MOBILE_PAGE_ACTIONS_ID),
    );
  });
  return slot;
}

export interface PageHeaderProps {
  /** Page title. */
  title: string;
  /** Inline badge shown next to the title (e.g. a "Running" status pill). */
  badge?: ReactNode;
  /** Secondary line under the title. */
  subtitle?: ReactNode;
  /** Controls aligned to the right of the header. */
  right?: ReactNode;
  /**
   * Keep `right` in the header on phones instead of lifting it into the top
   * bar. Use when the controls are wider than an icon or two — a search field,
   * a pair of buttons — since the top bar has only the space the page title
   * does not use.
   */
  stackRightOnMobile?: boolean;
  /** When set, renders a back-arrow button to the left of the title. */
  onBack?: () => void;
  className?: string;
}

/**
 * A page's sticky header: title, badge, subtitle and actions, with an
 * optional back arrow.
 *
 * On a phone the `MobileTopBar` already names the page, so the title and its
 * subtitle drop out there, and a compact action moves *into* the top bar,
 * beside the page name, instead of costing a whole row above the content.
 * Wide controls stay put (see `stackRightOnMobile`).
 *
 * `right` is rendered in ONE place, never twice and hidden with CSS: two
 * copies would mount any stateful control, and its effects, twice, and a
 * lookup that picks the first match (an onboarding spotlight) would land on
 * the hidden copy.
 */
export function PageHeader({
  title,
  badge,
  subtitle,
  right,
  stackRightOnMobile = false,
  onBack,
  className,
}: PageHeaderProps) {
  const isMobile = useIsMobile();
  const actionSlot = useMobileActionSlot();
  const liftsAction =
    Boolean(right) && !stackRightOnMobile && isMobile && actionSlot !== null;
  const keptOnMobile = Boolean(
    onBack || badge || (right && stackRightOnMobile),
  );

  return (
    <header
      className={cn(
        "sticky top-0 z-5 border-b border-line bg-surface px-4 py-2.5 md:px-7 md:py-3",
        !keptOnMobile && "hidden md:block",
        className,
      )}
    >
      {/* One row at every width: the title is hidden on phones, so the
          controls sit beside the back arrow rather than under an empty band. */}
      <div className="flex items-center justify-between gap-3">
        {/* On a phone this column holds only what the top bar does not: the
            back arrow and the badge. With neither, it is an empty flex item
            that would still claim the row's gap, so it is removed outright. */}
        <div
          className={cn(
            "min-w-0 items-center gap-2.5 md:flex md:gap-3",
            onBack || badge ? "flex" : "hidden",
          )}
        >
          {onBack && (
            <button
              type="button"
              onClick={onBack}
              aria-label="Back"
              className="-ml-2 inline-flex size-10 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors active:bg-muted/60 md:-ml-1 pointer-fine:size-8 md:hover:bg-muted/40 md:hover:text-foreground"
            >
              <ArrowLeft className="size-[18px]" strokeWidth={2} />
            </button>
          )}
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-2 md:gap-2.5">
              <h1 className="hidden truncate text-xl font-semibold tracking-tight text-foreground md:block">
                {title}
              </h1>
              {badge}
            </div>
            {subtitle && (
              // Truncates for the same reason the title does: this column is
              // `min-w-0` so the actions can compress it, and without a
              // single-line rule the subtitle reflows to one word per line
              // rather than shrinking.
              <p className="mt-px hidden truncate text-sm text-muted-foreground md:block">
                {subtitle}
              </p>
            )}
          </div>
        </div>
        {right && !liftsAction && (
          <div
            className={cn(
              "flex items-center gap-2 md:gap-3",
              // Must be allowed to compress rather than push the title
              // off-screen on a 360px phone.
              "min-w-0 shrink flex-wrap justify-end md:shrink-0 md:flex-nowrap",
            )}
          >
            {right}
          </div>
        )}
      </div>
      {/* The same element, moved — the slot is already a flex row with the
          right gap, and the bar it lives in is itself phone-only. */}
      {liftsAction && actionSlot ? createPortal(right, actionSlot) : null}
    </header>
  );
}

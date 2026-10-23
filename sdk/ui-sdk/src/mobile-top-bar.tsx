// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { Menu } from "lucide-react";
import { Slot } from "radix-ui";
import type { ReactElement, ReactNode } from "react";

import { cn } from "./lib/utils";

/**
 * Portal target for the current page's primary action ("New case", "Add
 * knowledge base"…). An app mounts exactly one MobileTopBar at a time, so a
 * single id is unambiguous. `PageHeader` portals its `right` here on a phone.
 */
export const MOBILE_PAGE_ACTIONS_ID = "mobile-page-actions";

export interface MobileTopBarProps {
  /** The current page's name. */
  title: ReactNode;
  /** Opens the navigation drawer (see `NavDrawer`). */
  onOpenSidebar: () => void;
  /** What the brand mark links through (a router `Link`); the mark is placed
   *  inside it. Without it the mark is not a link. */
  home?: ReactElement;
  imageSrc?: string;
  /** Trailing controls, aligned right. */
  right?: ReactNode;
  className?: string;
}

/**
 * The phone-only chrome bar: hamburger, brand mark, current page title, the
 * page's primary action, and an optional trailing slot. Hidden from `md` up,
 * where the sidebar and the page header carry the same information.
 *
 * Pinned to the top edge, so it carries the notch inset itself — the app shell
 * above it stays inset-free and full-bleed.
 */
export function MobileTopBar({
  title,
  onOpenSidebar,
  home,
  imageSrc = "/login.svg",
  right,
  className,
}: MobileTopBarProps) {
  const brand = (
    <img
      src={imageSrc}
      alt=""
      aria-hidden={true}
      className="size-7 shrink-0 rounded-sm"
    />
  );

  return (
    <div
      className={cn(
        "sticky top-0 z-30 shrink-0 border-b border-line bg-card/95 pt-[env(safe-area-inset-top)] ps-[env(safe-area-inset-left)] pe-[env(safe-area-inset-right)] backdrop-blur md:hidden",
        className,
      )}
    >
      <div className="flex h-14 items-center gap-2 px-2">
        <button
          type="button"
          onClick={onOpenSidebar}
          aria-label="Open navigation"
          // Onboarding hand-off anchor. The nav rows an onboarding step points
          // at live inside a drawer that is closed (and unmounted) on a phone,
          // so the spotlight aims here instead.
          data-onboarding="mobile-nav"
          className="inline-flex size-11 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors active:bg-muted/60"
        >
          <Menu className="size-5" strokeWidth={1.75} />
        </button>
        {home ? (
          // Padding grows the hit area to 44px; the matching negative margin
          // keeps the laid-out box at the mark's own 28px.
          <Slot.Root
            aria-label="Home"
            className="-m-2 shrink-0 rounded-lg p-2 active:bg-muted/60"
          >
            <Slot.Slottable>{home}</Slot.Slottable>
            {brand}
          </Slot.Root>
        ) : (
          brand
        )}
        <div className="min-w-0 flex-1 truncate text-[15px] font-semibold">
          {title}
        </div>
        {right && <div className="flex shrink-0 items-center">{right}</div>}
        {/* Where the current page's primary action lands. PageHeader portals
            into this rather than spending a row of a small screen on a button.
            Rendered unconditionally so the node exists before any page
            mounts; empty, it costs nothing. */}
        <div
          id={MOBILE_PAGE_ACTIONS_ID}
          className="flex shrink-0 items-center gap-1 empty:hidden"
        />
      </div>
    </div>
  );
}

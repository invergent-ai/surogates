// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { Drawer as DrawerPrimitive } from "vaul";

export interface NavDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Names the drawer for screen readers; it is not shown. */
  title?: string;
  children: ReactNode;
}

/**
 * The phone's navigation: a left-anchored drawer, swipe-dismissable, carrying
 * the same rows the desktop `Sidebar` does — drawn open, since outside the
 * `Sidebar` there is no rail to collapse.
 *
 * The drawer owns the device insets: the notch/home-indicator padding lives
 * here rather than on each row, so the same rows work in both frames. Width
 * is capped against the viewport so the scrim stays tappable — a full-bleed
 * panel leaves no way out but the back gesture.
 *
 * Closing on navigation is the app's job: it calls `onOpenChange(false)` from
 * its rows, or when the route changes.
 */
export function NavDrawer({
  open,
  onOpenChange,
  title = "Navigation",
  children,
}: NavDrawerProps) {
  return (
    <DrawerPrimitive.Root
      open={open}
      onOpenChange={onOpenChange}
      direction="left"
    >
      <DrawerPrimitive.Portal>
        <DrawerPrimitive.Overlay
          data-slot="nav-drawer-overlay"
          className="fixed inset-0 z-50 bg-black/20 data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0"
        />
        <DrawerPrimitive.Content
          data-slot="nav-drawer"
          // The name is the title; there is no description to point at.
          aria-describedby={undefined}
          className="fixed inset-y-0 left-0 z-50 flex h-auto w-[19.5rem] max-w-[86vw] flex-col rounded-none border-r bg-surface p-0 text-sm text-popover-foreground outline-none sm:max-w-xs"
        >
          {/* vaul builds on Radix Dialog, which requires an accessible name.
              The rows' own brand header is a link, not a heading, so the name
              is provided here for screen readers only. */}
          <DrawerPrimitive.Title className="sr-only">{title}</DrawerPrimitive.Title>
          <div className="relative flex h-full flex-col overflow-hidden bg-surface pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] ps-[env(safe-area-inset-left)]">
            {/* Swiping the panel away and tapping the scrim both close it,
                but neither is visible, and a drawer with no button to shut it
                reads as stuck. Sits over the free space beside the brand mark
                rather than taking a row of its own. */}
            <DrawerPrimitive.Close
              aria-label="Close navigation"
              className="absolute end-2 top-[calc(0.75rem+env(safe-area-inset-top))] z-10 inline-flex size-11 items-center justify-center rounded-xl text-muted-foreground transition-colors active:bg-foreground/[0.06]"
            >
              <X className="size-5" strokeWidth={1.75} />
            </DrawerPrimitive.Close>
            {children}
          </div>
        </DrawerPrimitive.Content>
      </DrawerPrimitive.Portal>
    </DrawerPrimitive.Root>
  );
}

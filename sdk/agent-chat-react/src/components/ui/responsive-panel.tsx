// A panel that is a popover with a cursor and a bottom sheet on a phone.
//
// The composer's secondary controls (tools, session context) open panels that
// are anchored above a ~32px button. That is right on a desktop and wrong on a
// phone: an anchored popover near the bottom of the screen has nowhere to go,
// its content is capped by whatever space is left above the composer, and the
// whole thing sits under the thumb that opened it. A sheet rising from the
// bottom edge has the full width, as much height as it needs, and lands where
// the hand already is.
//
// The two are different components, not one component with different classes,
// so the choice is made in JS — see useIsMobile.

import { Dialog as DialogPrimitive } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";
import { useIsMobile } from "../../lib/use-is-mobile";
import { cn } from "../../lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "./popover";

export interface ResponsivePanelProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The control that opens the panel. Rendered as the trigger in both forms. */
  trigger: ReactNode;
  /**
   * Names the sheet for screen readers. Radix Dialog requires an accessible
   * name; it is visually hidden, since the panel's own contents are labelled.
   */
  title: string;
  children: ReactNode;
  /** Popover-only; the sheet is always full width. */
  align?: ComponentProps<typeof PopoverContent>["align"];
  /**
   * Styling for the popover form only.
   *
   * Deliberately not a shared `className`: `cn` is tailwind-merge, so a class
   * written for the popover silently deletes a conflicting one from the
   * sheet's own base list. `p-0` and `pb-[env(safe-area-inset-bottom)]` are
   * the same conflict group, so passing the popover's `p-0` through dropped
   * the sheet's home-indicator inset — and a `divide-y` meant for the
   * popover's sections drew a rule under the sheet's grab handle instead.
   */
  popoverClassName?: string;
  /** Styling for the sheet form only. Same reasoning as popoverClassName. */
  sheetClassName?: string;
}

export function ResponsivePanel({
  open,
  onOpenChange,
  trigger,
  title,
  children,
  align = "start",
  popoverClassName,
  sheetClassName,
}: ResponsivePanelProps) {
  const isMobile = useIsMobile();

  if (!isMobile) {
    return (
      <Popover open={open} onOpenChange={onOpenChange}>
        <PopoverTrigger asChild>{trigger}</PopoverTrigger>
        <PopoverContent
          side="top"
          align={align}
          className={cn("w-64 overflow-hidden rounded-xl p-1", popoverClassName)}
        >
          {children}
        </PopoverContent>
      </Popover>
    );
  }

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Trigger asChild>{trigger}</DialogPrimitive.Trigger>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          data-slot="responsive-panel-overlay"
          className="fixed inset-0 z-50 bg-black/40 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0"
        />
        <DialogPrimitive.Content
          data-slot="responsive-panel-sheet"
          aria-describedby={undefined}
          className={cn(
            "fixed inset-x-0 bottom-0 z-50 flex max-h-[80svh] flex-col overflow-hidden rounded-t-2xl border-t border-border bg-popover pb-[env(safe-area-inset-bottom)] text-popover-foreground shadow-lg",
            "data-[state=closed]:animate-out data-[state=closed]:slide-out-to-bottom data-[state=open]:animate-in data-[state=open]:slide-in-from-bottom",
            sheetClassName,
          )}
        >
          <DialogPrimitive.Title className="sr-only">
            {title}
          </DialogPrimitive.Title>
          {/* Not draggable — Radix has no drag gesture and the sheet is short
              enough that a grab bar would only promise one. It reads as the
              handle the shape implies while staying purely decorative. */}
          <div
            aria-hidden="true"
            className="mx-auto mt-2.5 mb-1 h-1 w-9 shrink-0 rounded-full bg-muted-foreground/30"
          />
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-1">
            {children}
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

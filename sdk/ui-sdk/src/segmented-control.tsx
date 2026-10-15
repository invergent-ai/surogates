"use client"

import * as React from "react"

import { cn } from "./lib/utils"

export interface Segment<T extends string> {
  value: T
  label: React.ReactNode
  /** Accessible name when the label alone is too terse ("Dev" → "Dev workspace"). */
  ariaLabel?: string
}

/**
 * The Work console's mode switch (its Work / Dev toggle), for any two or
 * three peers. With a mouse it is a line of 13px words split by slashes, the
 * picked one an amber pill; on touch it becomes a full-width segmented
 * control at the 44px floor, equal halves on a round track. Every segment is
 * a button: the picked one is inert but stays focusable and announces its
 * state, which a `<span>` cannot.
 */
function SegmentedControl<T extends string>({
  segments,
  value,
  onChange,
  className,
  "aria-label": ariaLabel,
}: {
  segments: readonly Segment<T>[]
  value: T
  onChange: (value: T) => void
  className?: string
  "aria-label"?: string
}) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      data-slot="segmented-control"
      className={cn(
        "flex shrink-0 items-center gap-2 text-[13px]",
        "pointer-coarse:w-full pointer-coarse:gap-1 pointer-coarse:rounded-full pointer-coarse:bg-foreground/[0.06] pointer-coarse:p-1 pointer-coarse:text-sm",
        className
      )}
    >
      {segments.map((segment, index) => {
        const active = segment.value === value
        return (
          <React.Fragment key={segment.value}>
            {index > 0 && (
              <span aria-hidden={true} className="text-subtle pointer-coarse:hidden">
                /
              </span>
            )}
            <button
              type="button"
              data-slot="segmented-control-item"
              aria-label={segment.ariaLabel}
              aria-pressed={active}
              onClick={() => {
                if (!active) onChange(segment.value)
              }}
              className={cn(
                "cursor-pointer rounded-full border-none font-semibold transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/40",
                "pointer-coarse:inline-flex pointer-coarse:min-h-11 pointer-coarse:flex-1 pointer-coarse:items-center pointer-coarse:justify-center pointer-coarse:px-4",
                active
                  ? "bg-amber-400 px-3 py-1.25 text-stone-950 pointer-coarse:py-0"
                  : "bg-transparent text-subtle hover:text-foreground pointer-coarse:text-muted-foreground pointer-coarse:active:bg-foreground/[0.06]"
              )}
            >
              {segment.label}
            </button>
          </React.Fragment>
        )
      })}
    </div>
  )
}

export { SegmentedControl }

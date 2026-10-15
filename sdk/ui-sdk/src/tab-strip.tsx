import * as React from "react"
import { XIcon } from "lucide-react"

import { cn } from "./lib/utils"

/**
 * A compact strip of tabs that close (a browser's open pages), drawn as the
 * Work view switch draws its segments: muted text, a faint fill on the open
 * one. It switches nothing itself; each tab says what it does.
 */
function TabStrip({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      role="tablist"
      data-slot="tab-strip"
      className={cn("flex min-w-0 items-center gap-0.5 overflow-x-auto", className)}
      {...props}
    />
  )
}

/**
 * One tab, with an × once `onClose` is given. A `div` with the tab role, not
 * a button: the × is a button, and a button cannot hold another.
 */
function TabStripTab({
  active = false,
  onSelect,
  onClose,
  closeLabel = "Close tab",
  className,
  children,
  ...props
}: Omit<React.ComponentProps<"div">, "onSelect"> & {
  active?: boolean
  onSelect: () => void
  onClose?: () => void
  /** The ×'s accessible name. */
  closeLabel?: string
}) {
  return (
    <div
      role="tab"
      tabIndex={0}
      aria-selected={active}
      data-slot="tab-strip-tab"
      data-active={active}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault()
          onSelect()
        }
      }}
      className={cn(
        "flex h-7 min-h-11 max-w-40 shrink-0 cursor-pointer items-center gap-1 rounded-[7px] pl-2.5 text-[12px] transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/40 pointer-fine:min-h-0",
        onClose ? "pr-1" : "pr-2.5",
        active
          ? "bg-foreground/[0.08] font-medium text-foreground"
          : "text-muted-foreground hover:bg-foreground/[0.04] hover:text-foreground",
        className
      )}
      {...props}
    >
      <span className="min-w-0 truncate">{children}</span>
      {onClose && (
        <button
          type="button"
          data-slot="tab-strip-close"
          aria-label={closeLabel}
          onClick={(event) => {
            // Closing a tab is not opening it.
            event.stopPropagation()
            onClose()
          }}
          // The × is 16px; the overlay gives it a usable hit area.
          className="relative flex size-4 shrink-0 cursor-pointer items-center justify-center rounded-[4px] text-muted-foreground transition-colors outline-none after:absolute after:-inset-1.5 hover:bg-foreground/[0.08] hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40"
        >
          <XIcon className="size-3" />
        </button>
      )}
    </div>
  )
}

export { TabStrip, TabStripTab }

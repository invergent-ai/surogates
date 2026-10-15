import * as React from "react"
import { cva } from "class-variance-authority"
import { XIcon } from "lucide-react"

import { cn } from "./lib/utils"

export type Tone = "ok" | "warn" | "bad" | "muted"

// The Work console's status tones: a faint wash of the colour with text a
// step darker in light mode and a step lighter in dark.
export const TONE: Record<Tone, string> = {
  ok: "bg-emerald-500/12 text-emerald-700 dark:text-emerald-300",
  warn: "bg-amber-500/14 text-amber-700 dark:text-amber-300",
  bad: "bg-red-500/12 text-red-700 dark:text-red-300",
  muted: "bg-foreground/[0.06] text-muted-foreground",
}

// One chip for statuses (TonePill), tags and removable values (TagPill):
// round, medium weight, tinted by tone.
const chipVariants = cva(
  "inline-flex w-fit shrink-0 items-center gap-1 rounded-full font-medium whitespace-nowrap [&_svg]:pointer-events-none [&_svg]:shrink-0",
  {
    variants: {
      tone: TONE,
      size: {
        sm: "px-2 py-0.5 text-[11.5px]",
        default: "px-2 py-0.5 text-xs",
        lg: "px-2.5 py-0.5 text-sm",
      },
    },
    defaultVariants: { tone: "muted", size: "default" },
  }
)

function Chip({
  tone = "muted",
  size = "default",
  onRemove,
  removeLabel = "Remove",
  className,
  children,
  ...props
}: React.ComponentProps<"span"> & {
  tone?: Tone
  size?: "sm" | "default" | "lg"
  /** Shows an × that calls this. */
  onRemove?: () => void
  /** The ×'s accessible name. */
  removeLabel?: string
}) {
  return (
    <span
      data-slot="chip"
      data-tone={tone}
      className={cn(chipVariants({ tone, size }), onRemove && "pr-1", className)}
      {...props}
    >
      {children}
      {onRemove && (
        <button
          type="button"
          // preventDefault on mousedown keeps the adjacent input focused so
          // removing a chip doesn't trigger its blur-to-commit handler.
          onMouseDown={(e) => e.preventDefault()}
          onClick={onRemove}
          aria-label={removeLabel}
          // The × is 10px; the overlay gives it a usable hit area.
          className="relative inline-flex cursor-pointer items-center justify-center rounded-full border-none bg-transparent p-0.5 text-current opacity-60 transition-opacity outline-none after:absolute after:-inset-1.5 hover:opacity-100 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring/40"
        >
          <XIcon className="size-2.5" />
        </button>
      )}
    </span>
  )
}

/** A status chip ("Active", "Paused") in one of the tones. */
function TonePill({
  tone,
  className,
  children,
}: {
  tone?: Tone
  className?: string
  children: React.ReactNode
}) {
  return (
    <Chip tone={tone} size="sm" className={className}>
      {children}
    </Chip>
  )
}

/** A tag, removable when `onRemove` is given. */
function TagPill({
  label,
  onRemove,
  size = "xs",
  className,
}: {
  label: string
  onRemove?: () => void
  size?: "xs" | "sm"
  className?: string
}) {
  return (
    <Chip
      size={size === "xs" ? "default" : "lg"}
      onRemove={onRemove}
      removeLabel={`Remove tag ${label}`}
      className={cn("font-normal", className)}
    >
      {label}
    </Chip>
  )
}

export { Chip, TonePill, TagPill, chipVariants }

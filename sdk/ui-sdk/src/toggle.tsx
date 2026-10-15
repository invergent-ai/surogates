import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Toggle as TogglePrimitive } from "radix-ui"

import { cn } from "./lib/utils"

// The Work console's pressed-state toggles. `default` is the 9px chip its
// forms use for a choice that is on or off (Once / Repeating, the days of the
// week): a faint box, amber-tinted once pressed. `outline` is its filter
// pill: round, bordered, a neutral fill once on. Muted text brightens on
// hover. Touch keeps 40px or more, a mouse the compact height.
const toggleVariants = cva(
  "group/toggle inline-flex shrink-0 items-center justify-center gap-1.5 border whitespace-nowrap transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:pointer-events-none disabled:opacity-50 aria-invalid:border-destructive [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5",
  {
    variants: {
      variant: {
        default:
          "rounded-[9px] border-foreground/[0.08] bg-foreground/[0.04] font-medium text-muted-foreground hover:text-foreground data-[state=on]:border-amber-500/40 data-[state=on]:bg-amber-500/[0.12] data-[state=on]:text-foreground",
        outline:
          "rounded-full border-foreground/10 text-muted-foreground hover:text-foreground data-[state=on]:border-foreground/30 data-[state=on]:bg-foreground/[0.08] data-[state=on]:font-medium data-[state=on]:text-foreground",
      },
      size: {
        default: "min-h-10 px-3 text-[13px] pointer-fine:min-h-8",
        sm: "min-h-9 px-3 text-[12px] pointer-fine:min-h-7",
        lg: "min-h-11 px-4 text-sm pointer-fine:min-h-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Toggle({
  className,
  variant = "default",
  size = "default",
  ...props
}: React.ComponentProps<typeof TogglePrimitive.Root> &
  VariantProps<typeof toggleVariants>) {
  return (
    <TogglePrimitive.Root
      data-slot="toggle"
      className={cn(toggleVariants({ variant, size, className }))}
      {...props}
    />
  )
}

/**
 * The Work filter pill (a row of them filters a list: "All channels",
 * "Slack 3"…). An empty filter stays clickable, just quieter.
 */
function FilterPill({
  on,
  onClick,
  dim,
  className,
  ...props
}: Omit<
  React.ComponentProps<typeof TogglePrimitive.Root>,
  "pressed" | "onPressedChange" | "onClick"
> & {
  on: boolean
  onClick: () => void
  /** Nothing behind it yet: still clickable, just quieter. */
  dim?: boolean
}) {
  return (
    <Toggle
      variant="outline"
      size="sm"
      pressed={on}
      onPressedChange={() => onClick()}
      className={cn(dim && !on && "opacity-55", className)}
      {...props}
    />
  )
}

export { Toggle, FilterPill, toggleVariants }

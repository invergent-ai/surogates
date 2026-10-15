import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "./lib/utils"

// The ops console's own buttons, as its Work pages draw them: sentence case
// at 13px, 7px corners, and its amber as the default. Every size keeps the
// 44px touch minimum on a phone and its compact height with a mouse.
const buttonVariants = cva(
  "group/button inline-flex shrink-0 items-center justify-center gap-1.5 rounded-[7px] border border-transparent text-[13px] font-medium whitespace-nowrap transition-colors outline-none select-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:pointer-events-none disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/20 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default: "bg-amber-500 text-black hover:bg-amber-400",
        outline:
          "border-foreground/[0.12] bg-foreground/[0.04] text-foreground hover:bg-foreground/[0.07] aria-expanded:bg-foreground/[0.07]",
        secondary:
          "bg-foreground/[0.08] text-foreground hover:bg-foreground/[0.12] aria-expanded:bg-foreground/[0.12]",
        ghost:
          "text-foreground hover:bg-foreground/[0.06] aria-expanded:bg-foreground/[0.06]",
        destructive:
          "border-destructive/40 bg-destructive/10 font-semibold text-destructive hover:bg-destructive/20 focus-visible:ring-destructive/30",
        // Work's text link (see text-link): amber, brighter on hover, no underline.
        link: "text-amber-500 hover:text-amber-400",
      },
      size: {
        default:
          "h-9 min-h-11 px-4 pointer-fine:min-h-0 has-data-[icon=inline-end]:pr-3 has-data-[icon=inline-start]:pl-3",
        xs: "h-7 min-h-11 gap-1 px-2.5 text-xs pointer-fine:min-h-0 [&_svg:not([class*='size-'])]:size-3",
        sm: "h-8 min-h-11 px-3 pointer-fine:min-h-0",
        lg: "h-10 min-h-11 px-[18px] pointer-fine:min-h-0",
        icon: "size-9 min-h-11 min-w-11 pointer-fine:min-h-0 pointer-fine:min-w-0",
        "icon-xs":
          "size-7 min-h-11 min-w-11 pointer-fine:min-h-0 pointer-fine:min-w-0 [&_svg:not([class*='size-'])]:size-3",
        "icon-sm": "size-8 min-h-11 min-w-11 pointer-fine:min-h-0 pointer-fine:min-w-0",
        "icon-lg": "size-10 min-h-11 min-w-11 pointer-fine:min-h-0 pointer-fine:min-w-0",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }

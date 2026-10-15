import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "./lib/utils"

// The Work console's text links. `default` is its amber link ("Manage
// channels", "View all →"), `muted` the quiet one that brightens on hover
// ("Cancel", "Back"). No underline when standing alone: the colour carries
// it. A standalone link reaches 44px on touch without moving its text (the
// negative margin eats the padding); `inline` is for a link inside a
// sentence, underlined there so it reads as one.
const textLinkVariants = cva(
  "cursor-pointer font-medium transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5",
  {
    variants: {
      variant: {
        default: "text-amber-500 hover:text-amber-400",
        muted: "text-muted-foreground hover:text-foreground",
      },
      size: {
        default:
          "-mx-2 inline-flex min-h-11 w-fit items-center gap-1.5 rounded-lg px-2 text-[13px] pointer-fine:mx-0 pointer-fine:min-h-0 pointer-fine:px-0",
        sm: "-mx-2 inline-flex min-h-11 w-fit items-center gap-1 rounded-lg px-2 text-[12px] pointer-fine:mx-0 pointer-fine:min-h-0 pointer-fine:px-0",
        inline: "rounded-[2px] underline underline-offset-2",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function TextLink({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"a"> &
  VariantProps<typeof textLinkVariants> & {
    /** Render as the single child (a router link, a button). */
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot.Root : "a"

  return (
    <Comp
      data-slot="text-link"
      data-variant={variant}
      data-size={size}
      className={cn(textLinkVariants({ variant, size }), className)}
      {...props}
    />
  )
}

export { TextLink, textLinkVariants }

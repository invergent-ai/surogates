import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "./lib/utils"

// The Work console's counters and labels: a 6px box at 12px, sentence case,
// on a faint foreground fill. `default` is its unread/active count (amber on
// the fill), `secondary` its quiet count, `destructive` the bad tone,
// `outline` a bordered label, `ghost` bare muted text, `link` its amber link.
const badgeVariants = cva(
  "group/badge inline-flex w-fit shrink-0 items-center justify-center gap-1 overflow-hidden rounded-[6px] border border-transparent px-[7px] py-[2px] text-[12px] font-medium whitespace-nowrap tabular-nums transition-colors focus-visible:ring-2 focus-visible:ring-ring/40 outline-none aria-invalid:border-destructive [&>svg]:pointer-events-none [&>svg]:size-3!",
  {
    variants: {
      variant: {
        default: "bg-foreground/[0.08] text-amber-500 [a]:hover:bg-foreground/[0.12]",
        secondary:
          "bg-foreground/[0.08] text-muted-foreground [a]:hover:text-foreground",
        destructive:
          "bg-red-500/12 text-red-700 dark:text-red-300 [a]:hover:bg-red-500/20",
        outline:
          "border-foreground/15 text-muted-foreground [a]:hover:text-foreground",
        ghost: "px-0 text-muted-foreground hover:text-foreground",
        link: "px-0 text-amber-500 hover:text-amber-400",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function Badge({
  className,
  variant = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "span"

  return (
    <Comp
      data-slot="badge"
      data-variant={variant}
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  )
}

export { Badge, badgeVariants }

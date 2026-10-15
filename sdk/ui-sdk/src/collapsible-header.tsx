"use client"

import * as React from "react"
import { cva } from "class-variance-authority"
import { Collapsible as CollapsiblePrimitive } from "radix-ui"
import { ChevronDown, ChevronRight, type LucideIcon } from "lucide-react"

import { cn } from "./lib/utils"

// The Work console's two collapsible headers, as a styled CollapsibleTrigger
// (put it inside a `Collapsible`, above its `CollapsibleContent`):
//   nav     — a group in a list of rows ("Sessions" under an expanded agent):
//             the row's own shape, a right chevron that turns down when open.
//   section — a disclosure on a settings page ("What the agent knows about
//             them"): a 44px 13px header with a down chevron that flips.
//             Sits in a bordered box (`rounded-[10px] border
//             border-foreground/[0.08]`) whose content has a top rule.
const collapsibleHeaderVariants = cva(
  "group/collapsible-header flex w-full items-center text-left transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        nav: "gap-2.25 rounded-[7px] px-2 py-1.75 text-[13px] font-normal text-foreground/80 hover:bg-foreground/6 hover:text-foreground pointer-coarse:min-h-11 pointer-coarse:gap-2.5 pointer-coarse:rounded-lg pointer-coarse:px-2.5 pointer-coarse:text-[14px]",
        section:
          "min-h-11 gap-2 rounded-[10px] px-3 text-[13px] font-medium text-foreground",
      },
    },
    defaultVariants: { variant: "nav" },
  }
)

function CollapsibleHeader({
  icon: Icon,
  variant = "nav",
  className,
  children,
  ...props
}: React.ComponentProps<typeof CollapsiblePrimitive.CollapsibleTrigger> & {
  icon?: LucideIcon
  variant?: "nav" | "section"
}) {
  return (
    <CollapsiblePrimitive.CollapsibleTrigger
      data-slot="collapsible-header"
      data-variant={variant}
      className={cn(collapsibleHeaderVariants({ variant }), className)}
      {...props}
    >
      {Icon && (
        <Icon
          className="size-4 shrink-0 text-muted-foreground pointer-coarse:size-4.5"
          strokeWidth={1.75}
        />
      )}
      <span className="min-w-0 flex-1 text-left">{children}</span>
      {variant === "nav" ? (
        <ChevronRight
          aria-hidden={true}
          className="size-3.5 shrink-0 text-muted-foreground transition-transform group-aria-expanded/collapsible-header:rotate-90 pointer-coarse:size-4"
          strokeWidth={1.75}
        />
      ) : (
        <ChevronDown
          aria-hidden={true}
          className="size-4 shrink-0 text-muted-foreground transition-transform group-aria-expanded/collapsible-header:rotate-180"
        />
      )}
    </CollapsiblePrimitive.CollapsibleTrigger>
  )
}

export { CollapsibleHeader, collapsibleHeaderVariants }

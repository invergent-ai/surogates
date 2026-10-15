import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Tabs as TabsPrimitive } from "radix-ui"

import { cn } from "./lib/utils"

function Tabs({
  className,
  orientation = "horizontal",
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      data-orientation={orientation}
      className={cn(
        "group/tabs flex gap-2 data-horizontal:flex-col",
        className
      )}
      {...props}
    />
  )
}

// The ops console's tab bar: sentence-case pills, the active one on the
// chrome surface with an amber edge along its bottom. Both variants draw it;
// `default` sits the pills on a faint track, `line` on nothing.
const tabsListVariants = cva(
  "group/tabs-list inline-flex w-fit items-center gap-1.5 text-muted-foreground group-data-vertical/tabs:h-fit group-data-vertical/tabs:flex-col group-data-vertical/tabs:items-stretch",
  {
    variants: {
      variant: {
        default: "rounded-[12px] bg-foreground/[0.04] p-1",
        line: "bg-transparent",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function TabsList({
  className,
  variant = "default",
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List> &
  VariantProps<typeof tabsListVariants>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      data-variant={variant}
      className={cn(tabsListVariants({ variant }), className)}
      {...props}
    />
  )
}

// Bright along the bottom edge, fading upward round the corners: a ring cut
// out of a gradient by the mask; the blurred copy adds the bloom.
const RING_MASK: React.CSSProperties = {
  WebkitMask: "linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0)",
  WebkitMaskComposite: "xor",
  maskComposite: "exclude",
}

function TabsTrigger({
  className,
  children,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
        "group/tabs-trigger relative inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-[10px] px-3.5 text-sm font-medium whitespace-nowrap text-muted-foreground transition-colors outline-none pointer-fine:min-h-0 md:px-3 md:py-1.5 hover:bg-surface/60 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/40 disabled:pointer-events-none disabled:opacity-50 data-active:bg-surface data-active:text-foreground group-data-vertical/tabs:justify-start [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
        className
      )}
      {...props}
    >
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 hidden rounded-[10px] blur-[2px] group-data-active/tabs-trigger:block"
        style={{
          padding: "2px",
          background: "linear-gradient(to top, rgba(245,158,11,0.75) 10%, rgba(245,158,11,0) 40%)",
          ...RING_MASK,
        }}
      />
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 hidden rounded-[10px] group-data-active/tabs-trigger:block"
        style={{
          padding: "1px",
          background: "linear-gradient(to top, rgba(245,158,11,1) 10%, rgba(245,158,11,0) 40%)",
          ...RING_MASK,
        }}
      />
      <span className="relative inline-flex items-center gap-2">{children}</span>
    </TabsPrimitive.Trigger>
  )
}

function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      className={cn("flex-1 text-sm outline-none", className)}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger, TabsContent, tabsListVariants }

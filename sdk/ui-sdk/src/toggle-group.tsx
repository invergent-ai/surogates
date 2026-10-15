"use client"

import * as React from "react"
import { type VariantProps } from "class-variance-authority"
import { ToggleGroup as ToggleGroupPrimitive } from "radix-ui"

import { cn } from "./lib/utils"
import { toggleVariants } from "./toggle"

const ToggleGroupContext = React.createContext<
  VariantProps<typeof toggleVariants> & {
    spacing?: number
    orientation?: "horizontal" | "vertical"
  }
>({
  size: "default",
  variant: "default",
  spacing: 0,
  orientation: "horizontal",
})

// With spacing the items are separate toggles, drawn as `toggle` draws them.
// Joined (spacing 0) they are one segmented control, as the Work console
// draws its two: `default` is the Work / Dev switch — a round track with the
// picked segment an amber pill — and `outline` its view switch (Steps |
// Stats, Open | Resolved) — a 10px bordered track, the picked segment a
// faint fill.
const JOINED_TRACK = {
  default: "gap-1 rounded-full bg-foreground/[0.06] p-1",
  outline: "gap-0 rounded-[10px] border border-foreground/10 p-0.5",
} as const

const JOINED_ITEM = {
  default:
    "rounded-full border-transparent bg-transparent px-4 font-semibold text-muted-foreground hover:text-foreground data-[state=on]:border-transparent data-[state=on]:bg-amber-400 data-[state=on]:text-stone-950",
  outline:
    "rounded-[8px] border-transparent bg-transparent px-3 text-[12.5px] font-normal text-muted-foreground hover:text-foreground data-[state=on]:border-transparent data-[state=on]:bg-foreground/[0.08] data-[state=on]:font-medium data-[state=on]:text-foreground",
} as const

function ToggleGroup({
  className,
  variant,
  size,
  spacing = 0,
  orientation = "horizontal",
  children,
  ...props
}: React.ComponentProps<typeof ToggleGroupPrimitive.Root> &
  VariantProps<typeof toggleVariants> & {
    spacing?: number
    orientation?: "horizontal" | "vertical"
  }) {
  return (
    <ToggleGroupPrimitive.Root
      data-slot="toggle-group"
      data-variant={variant}
      data-size={size}
      data-spacing={spacing}
      data-orientation={orientation}
      style={{ "--gap": spacing } as React.CSSProperties}
      className={cn(
        // `w-fit` sizes to content and refuses to shrink, so a segmented
        // control wider than a phone dragged the whole page sideways rather
        // than containing itself. Capped at its container, scrolling inside
        // as a last resort — call sites should still shorten their labels.
        "group/toggle-group no-scrollbar flex w-fit max-w-full flex-row items-center gap-[--spacing(var(--gap))] overflow-x-auto data-vertical:flex-col data-vertical:items-stretch",
        spacing === 0 && JOINED_TRACK[variant ?? "default"],
        className
      )}
      {...props}
    >
      <ToggleGroupContext.Provider
        value={{ variant, size, spacing, orientation }}
      >
        {children}
      </ToggleGroupContext.Provider>
    </ToggleGroupPrimitive.Root>
  )
}

function ToggleGroupItem({
  className,
  children,
  variant = "default",
  size = "default",
  ...props
}: React.ComponentProps<typeof ToggleGroupPrimitive.Item> &
  VariantProps<typeof toggleVariants>) {
  const context = React.useContext(ToggleGroupContext)
  const itemVariant = context.variant || variant
  const itemSize = context.size || size

  return (
    <ToggleGroupPrimitive.Item
      data-slot="toggle-group-item"
      data-variant={itemVariant}
      data-size={itemSize}
      data-spacing={context.spacing}
      className={cn(
        "shrink-0 focus:z-10 focus-visible:z-10",
        toggleVariants({ variant: itemVariant, size: itemSize }),
        context.spacing === 0 && JOINED_ITEM[itemVariant ?? "default"],
        className
      )}
      {...props}
    >
      {children}
    </ToggleGroupPrimitive.Item>
  )
}

/**
 * A tab's two or three views (Steps | Stats, Members | Recent alerts): the
 * Work view switch. One view is always picked — clicking the picked one
 * again does not clear it.
 */
function ViewSwitch<T extends string>({
  views,
  value,
  onChange,
  label = "Show",
  className,
}: {
  views: readonly { id: T; label: React.ReactNode }[]
  value: T
  onChange: (v: T) => void
  label?: string
  className?: string
}) {
  return (
    <ToggleGroup
      type="single"
      variant="outline"
      aria-label={label}
      value={value}
      onValueChange={(v) => {
        if (v) onChange(v as T)
      }}
      className={className}
    >
      {views.map((v) => (
        <ToggleGroupItem key={v.id} value={v.id}>
          {v.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

/** The view last chosen under `key`, else the first; storage may be off. */
function useRememberedView<T extends string>(
  key: string,
  views: readonly T[]
): [T, (v: T) => void] {
  const [view, setView] = React.useState<T>(() => {
    try {
      const saved = localStorage.getItem(key)
      return views.find((v) => v === saved) ?? views[0]
    } catch {
      return views[0]
    }
  })
  const choose = (v: T) => {
    setView(v)
    try {
      localStorage.setItem(key, v)
    } catch {
      // A private window keeps the choice for this visit only.
    }
  }
  return [view, choose]
}

export { ToggleGroup, ToggleGroupItem, ViewSwitch, useRememberedView }

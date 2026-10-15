"use client"

import * as React from "react"
import { cva } from "class-variance-authority"
import { CheckIcon } from "lucide-react"

import { cn } from "./lib/utils"

// The Work console's selectable option cards.
//   default — the library pickers (skills, knowledge bases): a 12px card with
//             an icon tile, title, description and a round check; picked, it
//             takes an amber border and wash.
//   sm      — the exclusive mode cards (pricing model): tighter, the icon
//             tile itself turns amber, the border gets an amber ring; on a
//             phone each lies as a row, from `sm` up as a column.
// A toggle button (aria-pressed): pressing picks or unpicks it. For a set
// where exactly one is picked, have onClick select it and leave it pressed.
const choiceCardVariants = cva(
  "group/choice-card flex border text-left transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-not-allowed disabled:opacity-70",
  {
    variants: {
      size: {
        default: "flex-col gap-3 rounded-[12px] p-4",
        sm: "flex-row items-start gap-3 rounded-[10px] p-3.5 sm:flex-col sm:gap-2",
      },
      selected: { true: "", false: "" },
    },
    compoundVariants: [
      { size: "default", selected: true, className: "border-amber-500 bg-amber-500/[0.06]" },
      {
        size: "default",
        selected: false,
        className:
          "border-foreground/[0.08] bg-foreground/[0.02] hover:border-foreground/20 hover:bg-foreground/[0.04]",
      },
      {
        size: "sm",
        selected: true,
        className: "border-amber-500/70 bg-amber-500/[0.08] ring-1 ring-amber-500/70",
      },
      {
        size: "sm",
        selected: false,
        className: "border-foreground/[0.08] bg-foreground/[0.02] pointer-fine:hover:border-foreground/20",
      },
    ],
    defaultVariants: { size: "default", selected: false },
  }
)

function ChoiceCard({
  title,
  description,
  meta,
  icon,
  selected,
  size = "default",
  className,
  children,
  ...props
}: Omit<React.ComponentProps<"button">, "title"> & {
  title: React.ReactNode
  description?: React.ReactNode
  /** A line under the title (an id, a trigger), in mono. */
  meta?: React.ReactNode
  /** The tile's content: an icon, or an initial. */
  icon?: React.ReactNode
  selected: boolean
  size?: "default" | "sm"
}) {
  if (size === "sm") {
    return (
      <button
        type="button"
        data-slot="choice-card"
        data-size={size}
        aria-pressed={selected}
        className={cn(choiceCardVariants({ size, selected }), className)}
        {...props}
      >
        {icon && (
          <span
            className={cn(
              "flex size-7 shrink-0 items-center justify-center rounded-[7px] [&_svg:not([class*='size-'])]:size-4",
              selected
                ? "bg-amber-500 text-black"
                : "bg-foreground/[0.07] text-muted-foreground"
            )}
          >
            {icon}
          </span>
        )}
        <span className="flex min-w-0 flex-col gap-1 sm:contents">
          <span
            className={cn(
              "text-[14px] font-medium sm:text-[13px]",
              selected ? "text-foreground" : "text-foreground/85"
            )}
          >
            {title}
          </span>
          {description && (
            <span className="text-[12.5px] leading-[1.4] text-muted-foreground sm:text-[12px]">
              {description}
            </span>
          )}
          {children}
        </span>
      </button>
    )
  }

  return (
    <button
      type="button"
      data-slot="choice-card"
      data-size={size}
      aria-pressed={selected}
      className={cn(choiceCardVariants({ size, selected }), className)}
      {...props}
    >
      <span className="flex w-full items-start gap-3">
        {icon && (
          <span className="flex size-9 shrink-0 items-center justify-center rounded-[9px] border border-foreground/10 bg-foreground/[0.04] text-[13px] font-semibold text-foreground [&_svg:not([class*='size-'])]:size-4">
            {icon}
          </span>
        )}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[14px] font-semibold text-foreground">
            {title}
          </span>
          {meta && (
            <span className="mt-0.5 block truncate font-mono text-[11px] text-muted-foreground/70">
              {meta}
            </span>
          )}
        </span>
        <span
          aria-hidden={true}
          className={cn(
            "flex size-5 shrink-0 items-center justify-center rounded-full border transition-colors",
            selected
              ? "border-amber-500 bg-amber-500 text-black"
              : "border-foreground/20 text-transparent"
          )}
        >
          <CheckIcon className="size-3" strokeWidth={3} />
        </span>
      </span>
      {description && (
        <span className="line-clamp-2 text-[12.5px] leading-relaxed text-muted-foreground">
          {description}
        </span>
      )}
      {children}
    </button>
  )
}

export { ChoiceCard, choiceCardVariants }

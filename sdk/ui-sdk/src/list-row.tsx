import * as React from "react"
import { cva } from "class-variance-authority"
import { Slot } from "radix-ui"
import { ChevronRight, type LucideIcon } from "lucide-react"

import { cn } from "./lib/utils"
import { useSidebar } from "./sidebar"

// The Work console's list rows, in its three densities:
//   default — the navbar row (Agents, Sessions, Skills & Tools): a rounded
//             pill in muted text, filled faintly when hovered or active.
//   sm      — the row under an expanded agent (Overview, Configure, Inbox):
//             tighter, 13px, the label a step brighter than the icon.
//   lg      — the section-index row (agent sections, Configure on a phone):
//             a full-width row with a one-line description and a chevron,
//             meant for a bordered list (see ListGroup).
// Touch sizing keys off the pointer, not the width: a finger needs the 44px
// target at any screen size.
const listRowVariants = cva(
  "group/list-row flex w-full items-center text-left font-sans no-underline transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      size: {
        default:
          "gap-2.5 rounded-lg px-3 py-2.25 text-sm font-medium pointer-coarse:min-h-11 pointer-coarse:gap-3 pointer-coarse:rounded-xl pointer-coarse:px-3.5 pointer-coarse:text-[15px]",
        sm: "gap-2.25 rounded-[7px] px-2 py-1.75 text-[13px] pointer-coarse:min-h-11 pointer-coarse:gap-2.5 pointer-coarse:rounded-lg pointer-coarse:px-2.5 pointer-coarse:text-[14px]",
        lg: "min-h-14 gap-3 px-4 py-3 pointer-fine:min-h-0",
      },
      active: { true: "", false: "" },
    },
    compoundVariants: [
      { size: "default", active: true, className: "bg-foreground/5 text-foreground" },
      {
        size: "default",
        active: false,
        className: "text-muted-foreground hover:bg-foreground/5 hover:text-foreground",
      },
      { size: "sm", active: true, className: "bg-foreground/6 font-medium text-foreground" },
      {
        size: "sm",
        active: false,
        className: "font-normal text-foreground/80 hover:bg-foreground/6 hover:text-foreground",
      },
      { size: "lg", active: true, className: "bg-foreground/[0.04]" },
      {
        size: "lg",
        active: false,
        className: "hover:bg-foreground/[0.04] active:bg-foreground/[0.04]",
      },
    ],
    defaultVariants: { size: "default", active: false },
  }
)

const ICON_CLASS = {
  default: "size-4.5 pointer-coarse:size-5",
  // Quieter than the label while idle, the row's own colour once active.
  sm: "size-4 pointer-coarse:size-4.5 group-data-[active=false]/list-row:text-muted-foreground",
  lg: "size-5 text-muted-foreground",
} as const

type ListRowProps = Omit<React.ComponentProps<"button">, "children"> & {
  icon?: LucideIcon
  label: React.ReactNode
  /** One line under the label, so a list of sections is scannable. */
  description?: React.ReactNode
  /** Quiet text at the end of the row (a time, a size). */
  meta?: React.ReactNode
  /** A count at the end of the row, hidden when falsy (0 included). A string
   *  or number gets the muted pill; pass an element (e.g. an amber `Badge`)
   *  to draw it yourself. */
  badge?: React.ReactNode
  /** Anything else at the end of the row. Interactive content cannot live
   *  inside a button: render the row as a `div` (`asChild`) to hold it. */
  trailing?: React.ReactNode
  /** A chevron at the end: the row drills into something. */
  chevron?: boolean
  isActive?: boolean
  /** A pulsing activity dot on the icon (e.g. running workloads). */
  activityPulse?: boolean
  size?: "default" | "sm" | "lg"
  /** Render as the single child element (a router link, an `<a>`, a `div`)
   *  instead of a button. The row's content is placed inside it. */
  asChild?: boolean
  children?: React.ReactElement
}

/**
 * A single Work list entry: icon, label, optional description, and a
 * trailing count, meta text or chevron. A button by default; any link (or a
 * plain `div`) with `asChild`:
 *
 *   <ListRow asChild icon={Zap} label="Skills & Tools" isActive={active}>
 *     <Link to="/work/skills" />
 *   </ListRow>
 */
function ListRow({
  icon: Icon,
  label,
  description,
  meta,
  badge,
  trailing,
  chevron = false,
  isActive = false,
  activityPulse = false,
  size = "default",
  asChild = false,
  className,
  children,
  ...props
}: ListRowProps) {
  const Comp = asChild ? Slot.Root : "button"
  const isSmall = size !== "lg"
  // In a collapsed sidebar rail the row is its icon alone, centred, and the
  // label it no longer shows names it.
  const { collapsed } = useSidebar()

  return (
    <Comp
      data-slot="list-row"
      data-size={size}
      data-active={isActive}
      {...(asChild ? {} : { type: "button" as const })}
      aria-label={collapsed && typeof label === "string" ? label : undefined}
      className={cn(
        listRowVariants({ size, active: isActive }),
        !asChild && "cursor-pointer bg-transparent",
        collapsed && "justify-center gap-0 px-0 pointer-coarse:px-0",
        className
      )}
      {...props}
    >
      {asChild && <Slot.Slottable>{children}</Slot.Slottable>}
      {Icon && (
        <span className="relative flex shrink-0">
          <Icon className={ICON_CLASS[size]} strokeWidth={1.75} />
          {activityPulse && (
            <span className="absolute -top-1 -right-1 flex size-2">
              <span className="absolute inline-flex size-full animate-ping rounded-full bg-green-500 opacity-75" />
              <span className="relative inline-flex size-2 rounded-full bg-green-500" />
            </span>
          )}
        </span>
      )}
      {!collapsed && (
        <>
        {description ? (
          <span className="flex min-w-0 flex-1 flex-col">
            <span
              className={cn(
                "truncate",
                size === "lg" && "text-[15px] font-medium text-foreground"
              )}
            >
              {label}
            </span>
            <span className="truncate text-[12px] font-normal text-muted-foreground">
              {description}
            </span>
          </span>
        ) : (
          <span
            className={cn(
              "min-w-0 flex-1 truncate",
              size === "lg" && "text-[15px] font-medium text-foreground"
            )}
          >
            {label}
          </span>
        )}
        {meta && (
          <span className="shrink-0 text-[12px] font-normal text-muted-foreground">
            {meta}
          </span>
        )}
        {/* Falsy hides it: an empty string or a zero count says nothing. A
            ternary, not &&, or a 0 would render as text. */}
        {!badge ? null : typeof badge === "string" || typeof badge === "number" ? (
          <span
            className={cn(
              "shrink-0 rounded bg-foreground/10 px-1.5 py-px text-xs font-normal text-muted-foreground",
              isSmall && "pointer-coarse:rounded-md pointer-coarse:px-2 pointer-coarse:py-0.5 pointer-coarse:text-[13px]"
            )}
          >
            {badge}
          </span>
        ) : (
          badge
        )}
        {trailing}
        {chevron && (
          <ChevronRight
            aria-hidden={true}
            className="size-4 shrink-0 text-muted-foreground/60"
            strokeWidth={2}
          />
        )}
        </>
      )}
    </Comp>
  )
}

/**
 * The bordered list the `lg` rows sit in, with dividers inset past the icon
 * column. Pass the rows as children; each is wrapped in an `<li>`.
 */
function ListGroup({
  className,
  children,
  ...props
}: React.ComponentProps<"ul">) {
  return (
    <ul
      data-slot="list-group"
      className={cn(
        "overflow-hidden rounded-2xl border border-border bg-card",
        className
      )}
      {...props}
    >
      {React.Children.toArray(children).map((child, index) => (
        <li key={React.isValidElement(child) && child.key != null ? child.key : index}>
          {index > 0 && (
            <div aria-hidden="true" className="ml-[52px] h-px bg-foreground/[0.06]" />
          )}
          {child}
        </li>
      ))}
    </ul>
  )
}

export { ListRow, ListGroup, listRowVariants }

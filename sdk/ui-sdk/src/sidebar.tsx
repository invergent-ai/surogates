import * as React from "react"
import { Slot } from "radix-ui"
import { ChevronLeft, ChevronRight } from "lucide-react"

import { cn } from "./lib/utils"

const SidebarContext = React.createContext({ collapsed: false })

/**
 * Whether the enclosing rail is collapsed to its icons. Outside a `Sidebar`
 * (a phone drawer, a page) nothing is collapsed, so the same parts draw open
 * there.
 */
function useSidebar() {
  return React.useContext(SidebarContext)
}

type SidebarProps = Omit<
  React.ComponentProps<"aside">,
  "onMouseEnter" | "onMouseLeave"
> & {
  /** Pinned, the sidebar stays open; unpinned, it is an icon rail that opens
   *  while the pointer is over it. Kept here when not passed. */
  pinned?: boolean
  onPinnedChange?: (pinned: boolean) => void
}

/**
 * The ops console's desktop sidebar: 295px open, a 50px icon rail collapsed,
 * with the pin toggle along its bottom. Hidden below `md`, where navigation
 * belongs in a drawer: render the same parts there, outside a `Sidebar`.
 * `ListRow` and `SectionLabel` inside it shrink to their icon and rule.
 */
function Sidebar({
  pinned: pinnedProp,
  onPinnedChange,
  className,
  children,
  ...props
}: SidebarProps) {
  const [ownPinned, setOwnPinned] = React.useState(true)
  const pinned = pinnedProp ?? ownPinned
  const [hoverOpen, setHoverOpen] = React.useState(false)
  const collapsed = !pinned && !hoverOpen
  const context = React.useMemo(() => ({ collapsed }), [collapsed])

  function togglePinned() {
    // The toggle is under the pointer: unpinning closes the rail now rather
    // than leaving it hover-open until the pointer leaves.
    if (pinned) setHoverOpen(false)
    setOwnPinned(!pinned)
    onPinnedChange?.(!pinned)
  }

  return (
    <SidebarContext.Provider value={context}>
      <aside
        data-slot="sidebar"
        data-collapsed={collapsed ? "" : undefined}
        onMouseEnter={() => setHoverOpen(true)}
        onMouseLeave={() => setHoverOpen(false)}
        className={cn(
          "hidden h-full w-[295px] shrink-0 flex-col overflow-hidden border-r border-line bg-surface transition-[width] duration-200 ease-out data-collapsed:w-[50px] md:flex",
          className
        )}
        {...props}
      >
        <div className="flex min-h-0 flex-1 flex-col">{children}</div>
        <button
          type="button"
          onClick={togglePinned}
          aria-label={pinned ? "Collapse sidebar" : "Expand sidebar"}
          className="flex w-full shrink-0 cursor-pointer items-center justify-center border-t border-line py-2.5 text-faint transition-colors hover:text-foreground"
        >
          {pinned ? (
            <ChevronLeft className="size-4" strokeWidth={1.75} />
          ) : (
            <ChevronRight className="size-4" strokeWidth={1.75} />
          )}
        </button>
      </aside>
    </SidebarContext.Provider>
  )
}

type SidebarBrandProps = Omit<React.ComponentProps<"div">, "title"> & {
  title: string
  subtitle?: React.ReactNode
  imageSrc?: string
  /** What the mark and title link through (a router `Link`); they are placed
   *  inside it. */
  link?: React.ReactElement
}

/**
 * The sidebar's brand strip: the mark, a title and a muted subtitle; in the
 * rail the mark alone, centred. Children (a mode switch) follow it.
 */
function SidebarBrand({
  title,
  subtitle,
  imageSrc = "/login.svg",
  link,
  className,
  children,
  ...props
}: SidebarBrandProps) {
  const { collapsed } = useSidebar()
  const Comp = link ? Slot.Root : "div"

  return (
    // With a cursor the brand and its children share one line; on touch the
    // children get a row of their own.
    <div
      data-slot="sidebar-brand"
      className={cn(
        "flex shrink-0 flex-col gap-3 pt-5 pb-3 pointer-fine:flex-row pointer-fine:items-start pointer-fine:gap-2",
        collapsed ? "px-1.5" : "px-4",
        className
      )}
      {...props}
    >
      {/* A drawer's close button floats over the end of this line; the
          inset keeps the title clear of it. */}
      <Comp
        className={cn(
          "flex min-w-0 items-center gap-2.5 text-foreground no-underline pointer-coarse:pe-12",
          collapsed && "flex-1 justify-center gap-0"
        )}
      >
        {link && <Slot.Slottable>{link}</Slot.Slottable>}
        <img src={imageSrc} alt={title} className="size-9 shrink-0 rounded-full" />
        {!collapsed && (
          <div className="min-w-0 whitespace-nowrap">
            <div className="text-[15px] leading-tight font-semibold">{title}</div>
            {subtitle && <div className="text-xs text-muted-foreground">{subtitle}</div>}
          </div>
        )}
      </Comp>
      {children}
    </div>
  )
}

type SidebarUserBarProps = Omit<React.ComponentProps<"div">, "children"> & {
  avatar: React.ReactNode
  name: React.ReactNode
  /** The bar's `SidebarAction`s: theme, settings, sign out. */
  children?: React.ReactNode
}

/**
 * The sidebar's bottom bar: the user's avatar and name, then the global
 * controls. In the rail the avatar alone.
 */
function SidebarUserBar({
  avatar,
  name,
  className,
  children,
  ...props
}: SidebarUserBarProps) {
  const { collapsed } = useSidebar()

  return (
    <div
      data-slot="sidebar-user-bar"
      className={cn(
        "flex shrink-0 items-center gap-2.5 border-t border-line pt-3 pb-1 pointer-coarse:gap-1.5 pointer-coarse:pt-2 pointer-coarse:pb-0",
        // The left-weighted padding lines the avatar up with the rows above.
        collapsed ? "justify-center gap-0 px-1.5" : "pl-2 pr-1.5",
        className
      )}
      {...props}
    >
      {avatar}
      {!collapsed && (
        <>
          <span className="min-w-0 flex-1 truncate text-[13px] text-foreground pointer-coarse:text-[14px]">
            {name}
          </span>
          <div className="flex items-center gap-1.5">{children}</div>
        </>
      )}
    </div>
  )
}

/**
 * An icon control in the user bar. 26px with a cursor, the 44px floor on
 * touch. A button, or a router link via `asChild`.
 */
function SidebarAction({
  label,
  asChild = false,
  className,
  ...props
}: React.ComponentProps<"button"> & { label: string; asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      data-slot="sidebar-action"
      aria-label={label}
      {...(asChild ? {} : { type: "button" as const })}
      className={cn(
        "inline-flex cursor-pointer items-center justify-center rounded p-1 text-muted-foreground hover:bg-input hover:text-foreground pointer-coarse:size-11 pointer-coarse:rounded-xl pointer-coarse:p-0 pointer-coarse:active:bg-input [&_svg:not([class*='size-'])]:size-[18px]",
        className
      )}
      {...props}
    />
  )
}

export { Sidebar, SidebarBrand, SidebarUserBar, SidebarAction, useSidebar }

import * as React from "react"

import { cn } from "./lib/utils"
import { useSidebar } from "./sidebar"

/**
 * The Work navbar's group label ("Library"): a rule above a small, quiet
 * word, starting a new group in a list of rows. Touch steps the type up and
 * the spacing out with the rows around it.
 */
function SectionLabel({
  label,
  className,
  ...props
}: Omit<React.ComponentProps<"div">, "children"> & { label: React.ReactNode }) {
  // In a collapsed sidebar rail there is no room for the word: the rule alone
  // still says "a new group".
  const { collapsed } = useSidebar()

  return (
    <div
      data-slot="section-label"
      className={cn(
        "border-t border-line pointer-coarse:mt-3 pointer-coarse:mb-1 pointer-coarse:pt-4",
        collapsed ? "my-1.5" : "mt-2 mb-0.5 pt-3 pl-2.5",
        className
      )}
      {...props}
    >
      {!collapsed && (
        <span className="inline-block text-xs text-subtle pointer-coarse:text-[13px] pointer-coarse:font-medium pointer-coarse:tracking-wide">
          {label}
        </span>
      )}
    </div>
  )
}

export { SectionLabel }

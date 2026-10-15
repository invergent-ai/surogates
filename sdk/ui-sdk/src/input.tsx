import * as React from "react"

import { cn } from "./lib/utils"

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "h-10 w-full min-w-0 px-3.5 py-2 rounded-[10px] border border-foreground/10 bg-foreground/[0.04] text-sm text-foreground transition-colors outline-none placeholder:text-muted-foreground/50 focus-visible:border-foreground/20 focus-visible:bg-foreground/[0.06] disabled:cursor-not-allowed disabled:opacity-70 aria-invalid:border-destructive dark:aria-invalid:border-destructive/50 file:inline-flex file:h-7 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground disabled:pointer-events-none",
        className
      )}
      {...props}
    />
  )
}

export { Input }

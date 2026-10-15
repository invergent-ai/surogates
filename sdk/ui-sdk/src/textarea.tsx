import * as React from "react"

import { cn } from "./lib/utils"

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "flex field-sizing-content min-h-16 w-full resize-none px-3.5 py-2.5 rounded-[10px] border border-foreground/10 bg-foreground/[0.04] text-sm text-foreground transition-colors outline-none placeholder:text-muted-foreground/50 focus-visible:border-foreground/20 focus-visible:bg-foreground/[0.06] disabled:cursor-not-allowed disabled:opacity-70 aria-invalid:border-destructive dark:aria-invalid:border-destructive/50",
        className
      )}
      {...props}
    />
  )
}

export { Textarea }

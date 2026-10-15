import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "./lib/utils"
import { Separator } from "./separator"

// Buttons joined into one control: the inner corners square off and the
// shared borders collapse, so the group keeps the Work button's 7px corners
// only at its two ends.
const buttonGroupVariants = cva(
  "group/button-group flex w-fit items-stretch *:focus-visible:relative *:focus-visible:z-10 has-[>[data-slot=button-group]]:gap-2 *:data-[slot=input]:px-4 has-[>[data-variant=outline]]:*:data-[slot=input-group]:border-foreground/[0.12] has-[>[data-variant=outline]]:*:data-[slot=input-group]:px-2.5 has-[>[data-variant=outline]]:*:data-[slot=select-trigger]:border-foreground/[0.12] has-[>[data-variant=outline]]:*:[[role=combobox]]:px-2.5 has-[>[data-variant=outline]]:[&>[data-slot=input-group]:has(:focus-visible)]:border-foreground/20 has-[>[data-variant=outline]]:[&>[data-slot=select-trigger]:focus-visible]:border-foreground/20 has-[select[aria-hidden=true]:last-child]:[&>[data-slot=select-trigger]:last-of-type]:rounded-r-[10px] [&>[data-slot=select-trigger]:not([class*='w-'])]:w-fit [&>input]:flex-1 has-[>[data-variant=outline]]:[&>input]:border-foreground/[0.12] has-[>[data-variant=outline]]:[&>input:focus-visible]:border-foreground/20",
  {
    variants: {
      orientation: {
        horizontal:
          "[&>*:not(:first-child)]:rounded-l-none [&>*:not(:first-child)]:border-l-0 [&>*:not(:last-child)]:rounded-r-none",
        vertical:
          "flex-col [&>*:not(:first-child)]:rounded-t-none [&>*:not(:first-child)]:border-t-0 [&>*:not(:last-child)]:rounded-b-none",
      },
    },
    defaultVariants: {
      orientation: "horizontal",
    },
  }
)

function ButtonGroup({
  className,
  orientation,
  ...props
}: React.ComponentProps<"div"> & VariantProps<typeof buttonGroupVariants>) {
  return (
    <div
      role="group"
      data-slot="button-group"
      data-orientation={orientation}
      className={cn(buttonGroupVariants({ orientation }), className)}
      {...props}
    />
  )
}

// A label inside the group ("2 of 5"): the button's 13px medium in muted
// text, bordered like its outline neighbours.
function ButtonGroupText({
  className,
  asChild = false,
  ...props
}: React.ComponentProps<"div"> & {
  asChild?: boolean
}) {
  const Comp = asChild ? Slot.Root : "div"

  return (
    <Comp
      className={cn(
        "flex items-center gap-2 rounded-[7px] border border-transparent bg-transparent px-2.5 text-[13px] font-medium text-muted-foreground group-has-[>[data-variant=outline]]/button-group:border-foreground/[0.12] [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-3.5",
        className
      )}
      {...props}
    />
  )
}

function ButtonGroupSeparator({
  className,
  orientation = "vertical",
  ...props
}: React.ComponentProps<typeof Separator>) {
  return (
    <Separator
      data-slot="button-group-separator"
      orientation={orientation}
      className={cn(
        "relative self-stretch bg-foreground/[0.12] data-horizontal:mx-px data-horizontal:w-auto data-vertical:my-px data-vertical:h-auto",
        className
      )}
      {...props}
    />
  )
}

export {
  ButtonGroup,
  ButtonGroupSeparator,
  ButtonGroupText,
  buttonGroupVariants,
}

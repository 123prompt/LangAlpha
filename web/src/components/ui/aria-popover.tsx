"use client"

import {
  Dialog as AriaDialog,
  DialogProps as AriaDialogProps,
  DialogTrigger as AriaDialogTrigger,
  Popover as AriaPopover,
  PopoverProps as AriaPopoverProps,
  composeRenderProps,
} from "react-aria-components"

import { cn } from "@/lib/utils"

const PopoverTrigger = AriaDialogTrigger

// The Radix popover's layer and entrance, so the two kinds of floating panel
// stack and move alike. The nudge travels away from the trigger. The layer is
// a style, not a class: react-aria writes its own z-index inline, and only a
// style prop is spread after it.
const Popover = ({ className, style, offset = 4, ...props }: AriaPopoverProps) => (
  <AriaPopover
    offset={offset}
    style={composeRenderProps(style, (style) => ({ zIndex: 1030, ...style }))}
    className={composeRenderProps(className, (className) =>
      cn(
        "rounded-md border bg-popover text-popover-foreground shadow-md outline-none",
        "data-[placement=bottom]:[--pop-y:-0.5rem] data-[placement=top]:[--pop-y:0.5rem]",
        "data-[placement=left]:[--pop-x:0.5rem] data-[placement=right]:[--pop-x:-0.5rem]",
        "motion-safe:data-[entering]:animate-[pop-in_160ms_cubic-bezier(0.16,1,0.3,1)]",
        "motion-safe:data-[exiting]:animate-[pop-out_140ms_cubic-bezier(0.16,1,0.3,1)]",
        className
      )
    )}
    {...props}
  />
)

function PopoverDialog({ className, ...props }: AriaDialogProps) {
  return (
    <AriaDialog className={cn("p-4 outline outline-0", className)} {...props} />
  )
}

export { Popover, PopoverTrigger, PopoverDialog }

"use client"

import { Search } from "lucide-react"
import {
  Autocomplete,
  Input as AriaInput,
  SearchField as AriaSearchField,
  SearchFieldProps as AriaSearchFieldProps,
  composeRenderProps,
} from "react-aria-components"

import { cn } from "@/lib/utils"

interface SearchFieldProps extends AriaSearchFieldProps {
  placeholder?: string
}

/**
 * The search heading a popover's list, inside an `Autocomplete`. It holds
 * focus the whole time the list is open, so it draws no edge or ring of its
 * own: the rule beneath it is the only line between search and results.
 */
function SearchField({ className, placeholder, ...props }: SearchFieldProps) {
  return (
    <AriaSearchField
      className={composeRenderProps(className, (className) =>
        cn(
          "flex shrink-0 items-center gap-2 border-b border-[color:var(--color-border-muted)] px-3",
          className
        )
      )}
      {...props}
    >
      <Search
        aria-hidden="true"
        className="size-[15px] shrink-0 text-[color:var(--color-text-tertiary)]"
      />
      <AriaInput
        placeholder={placeholder}
        className={cn(
          "owns-its-edge h-10 min-w-0 flex-1 border-0 bg-transparent text-sm text-[color:var(--color-text-primary)] outline-none",
          "placeholder:text-[color:var(--color-text-tertiary)] [&::-webkit-search-cancel-button]:hidden"
        )}
      />
    </AriaSearchField>
  )
}

export { Autocomplete, SearchField }
export type { SearchFieldProps }

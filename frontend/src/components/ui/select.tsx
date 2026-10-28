"use client";

import * as SelectPrimitive from "@radix-ui/react-select";
import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export interface SelectOption {
  value: string;
  label: ReactNode;
  disabled?: boolean;
}

interface SelectProps {
  id?: string;
  value: string | undefined;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
}

function Chevron() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" className="text-faint">
      <path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}

export function Select({
  id,
  value,
  onValueChange,
  options,
  placeholder,
  disabled,
  className,
  ...aria
}: SelectProps) {
  return (
    <SelectPrimitive.Root value={value} onValueChange={onValueChange} disabled={disabled}>
      <SelectPrimitive.Trigger
        id={id}
        aria-label={aria["aria-label"]}
        className={cn(
          "inline-flex h-7 items-center justify-between gap-2 rounded-sm border border-line-strong",
          "bg-surface px-2 text-sm text-fg focus:outline-none data-[placeholder]:text-faint",
          "disabled:opacity-50",
          className,
        )}
      >
        <SelectPrimitive.Value placeholder={placeholder} />
        <SelectPrimitive.Icon>
          <Chevron />
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          sideOffset={2}
          className="z-50 min-w-[var(--radix-select-trigger-width)] overflow-hidden rounded-sm border border-line-strong bg-surface shadow-sm"
        >
          <SelectPrimitive.Viewport className="p-0.5">
            {options.map((option) => (
              <SelectPrimitive.Item
                key={option.value}
                value={option.value}
                disabled={option.disabled}
                className={cn(
                  "relative flex h-7 cursor-default select-none items-center rounded-xs px-2 text-sm",
                  "text-fg outline-none data-[highlighted]:bg-surface-2 data-[disabled]:opacity-50",
                  "data-[state=checked]:font-medium",
                )}
              >
                <SelectPrimitive.ItemText>{option.label}</SelectPrimitive.ItemText>
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
}

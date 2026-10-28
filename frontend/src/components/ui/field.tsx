import * as LabelPrimitive from "@radix-ui/react-label";
import {
  forwardRef,
  type InputHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";

import { cn } from "@/lib/cn";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...props }, ref) {
    return (
      <input
        ref={ref}
        className={cn(
          "h-7 w-full rounded-sm border border-line-strong bg-surface px-2 text-sm text-fg",
          "placeholder:text-faint focus:border-muted focus:outline-none",
          "disabled:cursor-not-allowed disabled:bg-surface-2 disabled:text-muted",
          className,
        )}
        {...props}
      />
    );
  },
);

interface FieldProps {
  id: string;
  label: string;
  hint?: ReactNode;
  error?: string | null;
  children: ReactNode;
  className?: string;
}

export function Field({ id, label, hint, error, children, className }: FieldProps) {
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <LabelPrimitive.Root htmlFor={id} className="text-xs font-medium text-muted">
        {label}
      </LabelPrimitive.Root>
      {children}
      {error ? (
        <p className="text-xs text-err">{error}</p>
      ) : hint ? (
        <p className="text-xs text-faint">{hint}</p>
      ) : null}
    </div>
  );
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...props }, ref) {
    return (
      <textarea
        ref={ref}
        className={cn(
          "w-full resize-y rounded-sm border border-line-strong bg-surface px-2 py-1.5 text-sm text-fg",
          "placeholder:text-faint focus:border-muted focus:outline-none",
          className,
        )}
        {...props}
      />
    );
  },
);

import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

interface PanelProps {
  title?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}

export function Panel({ title, meta, actions, children, className, bodyClassName }: PanelProps) {
  return (
    <section className={cn("rounded-sm border border-line bg-surface", className)}>
      {title || actions ? (
        <header className="flex h-8 items-center justify-between gap-3 border-b border-line px-3">
          <div className="flex min-w-0 items-baseline gap-2">
            <h2 className="truncate text-xs font-semibold uppercase tracking-wide text-muted">
              {title}
            </h2>
            {meta ? <span className="font-mono text-2xs text-faint">{meta}</span> : null}
          </div>
          {actions ? <div className="flex items-center gap-1">{actions}</div> : null}
        </header>
      ) : null}
      <div className={cn(bodyClassName)}>{children}</div>
    </section>
  );
}

export function EmptyRow({ children, colSpan }: { children: ReactNode; colSpan: number }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-3 py-6 text-center text-xs text-faint">
        {children}
      </td>
    </tr>
  );
}

export const th = "h-7 border-b border-line px-3 text-left text-2xs font-medium uppercase tracking-wide text-faint";
export const td = "h-8 border-b border-line px-3 align-middle";

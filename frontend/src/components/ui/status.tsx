import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export type Tone = "ok" | "warn" | "err" | "idle";

const dot: Record<Tone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  err: "bg-err",
  idle: "bg-faint",
};

const pill: Record<Tone, string> = {
  ok: "text-ok bg-ok-soft border-ok/30",
  warn: "text-warn bg-warn-soft border-warn/30",
  err: "text-err bg-err-soft border-err/30",
  idle: "text-muted bg-surface-2 border-line",
};

export function StatusDot({ tone, pulse = false }: { tone: Tone; pulse?: boolean }) {
  return (
    <span className="relative inline-flex h-1.5 w-1.5 shrink-0" aria-hidden="true">
      {pulse ? (
        <span className={cn("absolute inset-0 animate-ping rounded-full opacity-60", dot[tone])} />
      ) : null}
      <span className={cn("relative h-1.5 w-1.5 rounded-full", dot[tone])} />
    </span>
  );
}

export function StatusPill({
  tone,
  children,
  className,
}: {
  tone: Tone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center gap-1.5 rounded-xs border px-1.5 text-2xs font-medium",
        pill[tone],
        className,
      )}
    >
      <StatusDot tone={tone} />
      {children}
    </span>
  );
}

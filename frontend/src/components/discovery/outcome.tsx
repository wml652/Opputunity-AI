import { StatusPill, type Tone } from "@/components/ui/status";
import { cn } from "@/lib/cn";
import type { Outcome } from "@/lib/discovery-schemas";

const TONE: Record<Outcome, Tone> = {
  ELIGIBLE: "ok",
  UNCERTAIN: "warn",
  INELIGIBLE: "err",
  EXPIRED: "idle",
};

export function OutcomePill({ outcome }: { outcome: Outcome | null }) {
  if (!outcome) return <StatusPill tone="idle">Pending</StatusPill>;
  return <StatusPill tone={TONE[outcome]}>{outcome}</StatusPill>;
}

export function DeadlineCell({ iso }: { iso: string | null }) {
  if (!iso) return <span className="font-mono text-2xs text-faint">no deadline</span>;
  const date = new Date(iso);
  const days = Math.ceil((date.getTime() - Date.now()) / 86_400_000);
  const tone = days < 0 ? "text-faint line-through" : days <= 14 ? "text-warn" : "text-fg";
  return (
    <span className="flex flex-col leading-tight">
      <span className={cn("font-mono text-2xs tabular", tone)}>{date.toISOString().slice(0, 10)}</span>
      <span className="font-mono text-[10px] text-faint">{days < 0 ? "passed" : `${days}d left`}</span>
    </span>
  );
}

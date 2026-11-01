"use client";

import { cn } from "@/lib/cn";

export type SortKey = "match" | "deadline" | "title" | "status";
export type SortOrder = "asc" | "desc";

const KEYS: { value: SortKey; label: string }[] = [
  { value: "match", label: "Match score" },
  { value: "deadline", label: "Deadline" },
  { value: "title", label: "Title" },
  { value: "status", label: "Status" },
];

/** "Sort by" segmented control plus an ascending / descending toggle (server-side sorting). */
export function SortControls({
  sort,
  order,
  onChange,
}: {
  sort: SortKey;
  order: SortOrder;
  onChange: (sort: SortKey, order: SortOrder) => void;
}) {
  return (
    <div className="flex items-center gap-1">
      <span className="text-2xs text-faint">Sort by</span>
      <div className="flex gap-px rounded-sm border border-line bg-line">
        {KEYS.map((k) => (
          <button
            key={k.value}
            type="button"
            onClick={() => onChange(k.value, order)}
            className={cn(
              "h-6 px-2 text-2xs font-medium",
              sort === k.value ? "bg-inverse text-inverse-fg" : "bg-surface text-muted hover:text-fg",
            )}
          >
            {k.label}
          </button>
        ))}
      </div>
      <button
        type="button"
        onClick={() => onChange(sort, order === "asc" ? "desc" : "asc")}
        className="h-6 rounded-sm border border-line px-2 font-mono text-2xs text-muted hover:text-fg"
        title={order === "asc" ? "Ascending - click for descending" : "Descending - click for ascending"}
      >
        {order === "asc" ? "↑ ASC" : "↓ DESC"}
      </button>
    </div>
  );
}

/** 0-100 match score in mono, emerald only for strong matches. */
export function MatchScore({ score }: { score: number | null | undefined }) {
  if (score === null || score === undefined) return <span className="font-mono text-2xs text-faint">—</span>;
  return <span className={cn("font-mono text-xs tabular", score >= 70 ? "text-ok" : score < 40 ? "text-faint" : "text-fg")}>{score}</span>;
}

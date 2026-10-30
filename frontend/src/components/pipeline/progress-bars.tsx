"use client";

import { cn } from "@/lib/cn";
import type { CampaignT, ProgressT } from "@/lib/discovery-schemas";
import { useEventStream } from "@/lib/event-stream";

const PHASE_NAME: Record<string, string> = {
  PENDING: "Pending",
  CRAWLING: "Crawl · extract · evaluate",
  EXTRACTING: "Finishing extraction",
  EVALUATING: "Evaluating",
  QUEUED: "Queued",
  FILLING: "Auto-filling",
  AWAITING_OK: "Awaiting OK",
  COMPLETED: "Completed",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

function Bar({ pct, tone }: { pct: number; tone: "run" | "ok" | "err" | "idle" }) {
  const width = Math.max(0, Math.min(100, pct));
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-xs border border-line bg-surface-2"
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(width)}
    >
      <div
        className={cn(
          "h-full transition-[width] duration-700 ease-out",
          tone === "ok" && "bg-ok",
          tone === "err" && "bg-err",
          tone === "run" && "bg-fg",
          tone === "idle" && "bg-faint",
        )}
        style={{ width: `${width}%` }}
      />
    </div>
  );
}

function Pct({ value }: { value: number }) {
  return <span className="w-14 shrink-0 text-right font-mono text-sm font-medium tabular">{value.toFixed(1)}%</span>;
}

function Badge({ label, pct, extra }: { label: string; pct: number; extra?: string }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-xs border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted tabular">
      <span className="text-faint">{label}</span>
      <span className={cn(pct >= 100 && "text-ok")}>{pct.toFixed(0)}%</span>
      {extra ? <span className="text-faint">{extra}</span> : null}
    </span>
  );
}

/**
 * Two live progress bars for a campaign: the current process (crawl / extraction / auto-fill,
 * with the URL or application being worked on) and the weighted end-to-end pipeline.
 * Streamed `campaign.progress` events override the last fetched snapshot.
 */
export function ProgressBars({ campaign }: { campaign: CampaignT | null }) {
  const { progress: live } = useEventStream();
  if (!campaign) {
    return (
      <div className="rounded-sm border border-line bg-surface px-3 py-2 text-xs text-faint">
        No campaign yet. Launch one to see live progress.
      </div>
    );
  }
  const streamed = live[campaign.id]?.progress;
  // A finished campaign's stored snapshot is authoritative; while running, prefer the stream.
  const p: ProgressT | undefined =
    campaign.status === "RUNNING" ? (streamed ?? campaign.progress) : (campaign.progress ?? streamed);
  if (!p) return null;
  const failed = campaign.status === "FAILED";
  const done = p.overall_pct >= 100;
  const running = campaign.status === "RUNNING";
  const tone = failed ? "err" : done ? "ok" : running ? "run" : "idle";
  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-sm border border-line bg-line">
      <div className="flex flex-col gap-1.5 bg-surface px-3 py-2">
        <div className="flex items-baseline gap-2">
          <span className="text-2xs font-medium uppercase tracking-wide text-faint">Current process</span>
          <span className="truncate text-xs font-medium">{PHASE_NAME[p.phase] ?? p.phase}</span>
          <span className="truncate text-xs text-muted">· {p.current.label}</span>
          <span className="ml-auto shrink-0 font-mono text-2xs text-muted tabular">
            {p.current.done.toLocaleString()} / {p.current.total.toLocaleString()}
          </span>
          <Pct value={p.current.pct} />
        </div>
        <Bar pct={p.current.pct} tone={failed ? "err" : p.current.pct >= 100 ? "ok" : running ? "run" : "idle"} />
        <div className="truncate font-mono text-[10px] text-faint" title={p.current.detail ?? undefined}>
          {p.current.detail ?? (running ? "working" : campaign.name)}
        </div>
      </div>
      <div className="flex flex-col gap-1.5 bg-surface px-3 py-2">
        <div className="flex items-baseline gap-2">
          <span className="text-2xs font-medium uppercase tracking-wide text-faint">Overall pipeline</span>
          <span className="truncate text-xs text-muted">
            discovery → extraction & eligibility{p.auto_stage ? " → auto-fill & staging" : ""}
          </span>
          <span className="ml-auto" />
          <Pct value={p.overall_pct} />
        </div>
        <Bar pct={p.overall_pct} tone={tone} />
        <div className="flex flex-wrap gap-1">
          <Badge label="Crawled" pct={p.crawled_pct} extra={`${p.crawled} pages`} />
          <Badge label="Evaluated" pct={p.evaluated_pct} extra={`${p.evaluated}/${p.found}`} />
          {p.auto_stage ? (
            <Badge label="Auto-filled / staged" pct={p.staged_pct} extra={`${p.staged}/${p.stage_target}`} />
          ) : (
            <span className="font-mono text-[10px] text-faint">auto-stage off for this run</span>
          )}
        </div>
      </div>
    </div>
  );
}

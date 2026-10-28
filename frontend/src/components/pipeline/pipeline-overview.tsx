"use client";

import Link from "next/link";

import { CampaignLauncher } from "@/components/discovery/campaign-launcher";
import { DeadlineCell } from "@/components/discovery/outcome";
import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { StatusPill } from "@/components/ui/status";
import { cn, formatTime } from "@/lib/cn";
import { usePipelineSummary, useQueue } from "@/lib/discovery-queries";
import { useEventStream } from "@/lib/event-stream";
import { llmStatusPayloadSchema, type PipelineEvent } from "@/lib/schemas";

const COUNTERS = [
  { key: "websites_crawled", label: "Websites Crawled" },
  { key: "opportunities_found", label: "Opportunities Found" },
  { key: "eligible", label: "Eligible", tone: "ok" as const },
  { key: "needs_input", label: "Needs Input", tone: "warn" as const },
  { key: "ready_for_ok", label: "Ready for OK", tone: "warn" as const },
  { key: "submitted", label: "Submitted", tone: "ok" as const },
] as const;

// Campaign phases (M3) followed by the application phases arriving in M4/M5.
const PHASES = [
  { key: "CRAWLING", label: "Crawling" },
  { key: "EXTRACTING", label: "Extracting" },
  { key: "EVALUATING", label: "Evaluating" },
  { key: "QUEUED", label: "Queued" },
  { key: "FILLING", label: "Filling" },
  { key: "AWAITING_OK", label: "Awaiting OK" },
  { key: "SUBMITTED", label: "Submitted" },
];

function describe(event: PipelineEvent): { summary: string; tone?: "ok" | "err" | "warn" } {
  const p = event.payload as Record<string, unknown>;
  switch (event.type) {
    case "llm.status": {
      const s = llmStatusPayloadSchema.safeParse(event.payload);
      if (!s.success) break;
      return s.data.ok
        ? { summary: `${s.data.label} · ${s.data.model} · ${s.data.latency_ms} ms`, tone: "ok" }
        : { summary: `${s.data.label} · ${s.data.model} · ${s.data.error_code}`, tone: "err" };
    }
    case "llm.config_changed":
      return { summary: "Provider configuration updated" };
    case "campaign.created":
      return { summary: `Campaign created: ${String(p.name)}` };
    case "campaign.phase":
      return {
        summary: `Phase -> ${String(p.phase)}`,
        tone: p.phase === "FAILED" ? "err" : p.phase === "COMPLETED" ? "ok" : undefined,
      };
    case "page.crawled":
      return {
        summary: `${String(p.status)} ${String(p.url)}${Number(p.injection_flags) ? ` · ${String(p.injection_flags)} injection flag(s)` : ""}`,
        tone: p.status === "BLOCKED_SSRF" || p.status === "FAILED" ? "err" : undefined,
      };
    case "search.results":
      return { summary: `${String(p.hits)} results via ${String(p.provider)}: ${String(p.query)}` };
    case "opportunity.found":
      return { summary: `Found: ${String(p.title)}${p.deadline ? ` (deadline ${String(p.deadline)})` : ""}`, tone: "ok" };
    case "eligibility.verdict":
      return {
        summary: `${String(p.outcome)}: ${String(p.title)}`,
        tone: p.outcome === "ELIGIBLE" ? "ok" : p.outcome === "UNCERTAIN" ? "warn" : undefined,
      };
    case "application.queued":
      return { summary: `Queued: ${String(p.title)}`, tone: "ok" };
    case "vault.profile_updated":
      return { summary: "Student profile saved (PII encrypted)", tone: "ok" };
    case "vault.document_added":
      return { summary: `Document stored: ${String(p.kind)} · ${String(p.pages)} pages`, tone: "ok" };
    case "essay.fingerprint_updated":
      return { summary: `Fingerprint v${String(p.version)} · burstiness ${Number(p.burstiness).toFixed(3)}`, tone: "ok" };
    case "memory.learned":
      return { summary: "New answer learned into semantic memory", tone: "ok" };
  }
  return { summary: JSON.stringify(event.payload).slice(0, 160) };
}

export function PipelineOverview() {
  const { events, state, lastSeq } = useEventStream();
  const { data: summary } = usePipelineSummary();
  const campaign = summary?.latest_campaign ?? null;
  const running = campaign?.status === "RUNNING";
  const phaseIndex = campaign ? PHASES.findIndex((p) => p.key === campaign.phase) : -1;
  const doneThrough = campaign?.phase === "COMPLETED" ? 3 : phaseIndex - 1;

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-6 gap-px overflow-hidden rounded-sm border border-line bg-line">
        {COUNTERS.map((c) => {
          const value = summary ? summary[c.key] : 0;
          return (
            <div key={c.key} className="flex flex-col gap-1 bg-surface px-3 py-2">
              <span className="text-2xs font-medium uppercase tracking-wide text-faint">{c.label}</span>
              <span
                className={cn(
                  "font-mono text-xl font-medium tabular",
                  value > 0 && "tone" in c && c.tone === "warn" && "text-warn",
                  value > 0 && "tone" in c && c.tone === "ok" && "text-ok",
                )}
              >
                {value}
              </span>
            </div>
          );
        })}
      </div>

      <Panel
        title="Campaign phase"
        meta={campaign ? `${campaign.name} · ${campaign.status.toLowerCase()}` : "no campaign yet"}
        actions={
          campaign ? (
            <Link href="/campaigns" className="text-2xs text-muted underline-offset-2 hover:underline">
              crawl log
            </Link>
          ) : null
        }
      >
        <ol className="flex items-center px-3 py-2">
          {PHASES.map((phase, i) => {
            const current = running && i === phaseIndex;
            const done = i <= doneThrough;
            return (
              <li key={phase.key} className="flex flex-1 items-center gap-2">
                <span
                  className={cn(
                    "flex h-5 w-5 shrink-0 items-center justify-center rounded-xs border font-mono text-2xs",
                    current && "border-warn bg-warn-soft text-warn",
                    done && "border-ok bg-ok-soft text-ok",
                    !current && !done && "border-line-strong text-faint",
                  )}
                >
                  {i + 1}
                </span>
                <span className={cn("truncate text-xs", current ? "font-medium text-fg" : done ? "text-fg" : "text-muted")}>
                  {phase.label}
                </span>
                {i < PHASES.length - 1 ? <span className={cn("h-px flex-1", done ? "bg-ok/50" : "bg-line")} /> : null}
              </li>
            );
          })}
        </ol>
        {campaign?.status === "FAILED" ? (
          <p className="border-t border-line bg-err-soft px-3 py-1.5 text-xs text-err">{campaign.error}</p>
        ) : null}
      </Panel>

      <CampaignLauncher compact />

      <div className="grid grid-cols-5 gap-3">
        <Panel title="Live event log" meta={`seq ${lastSeq}`} className="col-span-3"
          actions={
            <StatusPill tone={state === "live" ? "ok" : "warn"}>{state === "live" ? "Streaming" : "Reconnecting"}</StatusPill>
          }
        >
          <div className="max-h-[420px] overflow-auto">
            <table className="w-full table-fixed border-collapse">
              <thead className="sticky top-0 bg-surface">
                <tr>
                  <th className={cn(th, "w-16")}>Seq</th>
                  <th className={cn(th, "w-20")}>Time</th>
                  <th className={cn(th, "w-40")}>Type</th>
                  <th className={th}>Detail</th>
                </tr>
              </thead>
              <tbody>
                {events.length === 0 ? (
                  <EmptyRow colSpan={4}>Waiting for events. Launch a campaign to watch the pipeline live.</EmptyRow>
                ) : (
                  events.map((event) => {
                    const { summary: text, tone } = describe(event);
                    return (
                      <tr key={event.seq} className="hover:bg-surface-2">
                        <td className={cn(td, "font-mono text-2xs text-faint tabular")}>{event.seq}</td>
                        <td className={cn(td, "font-mono text-2xs text-muted tabular")}>{formatTime(event.created_at)}</td>
                        <td className={cn(td, "truncate font-mono text-2xs")}>{event.type}</td>
                        <td className={cn(td, "truncate text-xs", tone === "ok" && "text-ok", tone === "err" && "text-err", tone === "warn" && "text-warn")} title={text}>
                          {text}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </Panel>
        <QueuePanel />
      </div>
    </div>
  );
}

function QueuePanel() {
  const { data: queue } = useQueue();
  return (
    <Panel title="Application queue" meta="deadline ASC" className="col-span-2">
      <table className="w-full table-fixed border-collapse">
        <thead>
          <tr>
            <th className={cn(th, "w-10")}>#</th>
            <th className={th}>Opportunity</th>
            <th className={cn(th, "w-24")}>Deadline</th>
          </tr>
        </thead>
        <tbody>
          {!queue?.length ? (
            <EmptyRow colSpan={3}>Eligible opportunities are queued here automatically.</EmptyRow>
          ) : (
            queue.map((q) => (
              <tr key={q.application_id} className="hover:bg-surface-2">
                <td className={cn(td, "font-mono text-2xs text-faint tabular")}>{q.priority_rank ?? "—"}</td>
                <td className={cn(td, "py-1")}>
                  <div className="truncate text-xs">{q.opportunity.title}</div>
                  <div className="font-mono text-[10px] text-faint">{q.state}</div>
                </td>
                <td className={td}><DeadlineCell iso={q.deadline_at} /></td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </Panel>
  );
}

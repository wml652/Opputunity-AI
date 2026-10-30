"use client";

import Link from "next/link";

import { CampaignLauncher } from "@/components/discovery/campaign-launcher";
import { ProgressBars } from "@/components/pipeline/progress-bars";
import { DeadlineCell } from "@/components/discovery/outcome";
import { Button } from "@/components/ui/button";
import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { StatusPill } from "@/components/ui/status";
import { cn, formatTime } from "@/lib/cn";
import { usePipelineSummary, useQueue } from "@/lib/discovery-queries";
import { useEventStream } from "@/lib/event-stream";
import { useBatchPrepare, useStartFilling } from "@/lib/m4-queries";
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
  // Streaming pipeline: pages are extracted, evaluated and queued while crawling continues.
  { key: "CRAWLING", label: "Crawl · extract · evaluate" },
  { key: "EXTRACTING", label: "Finishing extraction" },
  { key: "EVALUATING", label: "Evaluated" },
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
    case "application.state": {
      const st = String(p.state);
      return {
        summary: `${st}: ${String(p.title)}${p.reason ? ` · ${String(p.reason)}` : ""}`,
        tone:
          st === "PAUSED_NEEDS_USER_INPUT" || st === "AWAITING_FINAL_VERIFICATION"
            ? "warn"
            : st === "BLOCKED_NEEDS_HUMAN" || st === "FAILED"
              ? "err"
              : st === "SUBMITTED"
                ? "ok"
                : undefined,
      };
    }
    case "essay.draft_created":
      return {
        summary: `Essay draft · voice ${Math.round(Number(p.voice_match) * 100)}% · burstiness ${Math.round(Number(p.burstiness) * 100)}%${p.requires_manual ? " · manual authoring required" : ""}`,
        tone: p.requires_manual ? "warn" : "ok",
      };
    case "page.skipped":
      return { summary: `Skipped ${String(p.url)}${p.message ? ` · ${String(p.message)}` : ""}` };
    case "page.adapted":
      return { summary: `Adapted ${String(p.domain)}: ${String(p.message)}`, tone: "warn" };
    case "page.retrying":
      return { summary: `Retrying ${String(p.url)} (${String(p.reason)}) with an escalated strategy`, tone: "warn" };
    case "application.submission":
      return {
        summary: `Submission ${String(p.outcome).toLowerCase()}: ${String(p.title)}`,
        tone: p.outcome === "SUBMITTED" ? "ok" : p.outcome === "RESTAGED" ? "warn" : "err",
      };
    case "source.failed":
      return { summary: `Source ${String(p.source)} failed: ${String(p.error)} (campaign continues)`, tone: "err" };
    case "session.captured":
      return { summary: `Browser session saved for ${String(p.domain)}`, tone: "ok" };
    case "session.failed":
      return { summary: `Browser session for ${String(p.domain)} not saved: ${String(p.error)}`, tone: "err" };
    case "page.retried":
      return {
        summary: `Retried ${String(p.url)} -> ${String(p.status)}${p.error_code ? ` (${String(p.error_code)})` : ""}`,
        tone: p.status === "EXTRACTED" ? "ok" : p.status === "FAILED" ? "err" : undefined,
      };
    case "vault.profile_updated":
      return { summary: "Student profile saved (PII encrypted)", tone: "ok" };
    case "vault.document_added":
      return { summary: `Document stored: ${String(p.kind)} · ${String(p.pages)} pages`, tone: "ok" };
    case "essay.fingerprint_updated":
      return { summary: `Fingerprint v${String(p.version)} · burstiness ${Number(p.burstiness).toFixed(3)}`, tone: "ok" };
    case "applications.auto_staged": {
      const states = (p.states ?? {}) as Record<string, number>;
      const parts = Object.entries(states).map(([s, n]) => `${n} ${s.toLowerCase().replaceAll("_", " ")}`);
      return {
        summary: `Auto-staged ${String(p.count)} application(s) up to the final OK gate${parts.length ? ` · ${parts.join(", ")}` : ""}`,
        tone: "ok",
      };
    }
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
  const counters = campaign?.counters ?? {};
  const staged = (counters.stage_target ?? 0) > 0;
  const awaitingOk = (counters.stage_ready ?? 0) > 0 && ["COMPLETED", "AWAITING_OK"].includes(campaign?.phase ?? "");
  // COMPLETED after an auto-stage pass: Queued and Filling are done; Awaiting OK needs the user.
  const doneThrough =
    campaign?.phase === "COMPLETED" || campaign?.phase === "AWAITING_OK"
      ? staged
        ? 4
        : 3
      : phaseIndex - 1;

  return (
    <div className="flex flex-col gap-3">
      <ProgressBars campaign={campaign} />
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
            const current = (running && i === phaseIndex) || (awaitingOk && phase.key === "AWAITING_OK");
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
  const start = useStartFilling();
  const batch = useBatchPrepare();
  const queued = queue?.filter((q) => q.state === "QUEUED" && q.opportunity.portal_url).length ?? 0;
  return (
    <Panel
      title="Application queue"
      meta="deadline ASC"
      className="col-span-2"
      actions={
        <Button
          size="sm"
          onClick={() => batch.mutate(Math.min(5, queued))}
          disabled={batch.isPending || queued === 0}
          title="Fill the 5 closest-deadline queued applications up to the final OK gate. Never submits."
        >
          {batch.isPending ? "Staging" : "Auto-Stage Eligible Queue"}
        </Button>
      }
    >
      {batch.data ? (
        <p className="border-b border-line px-3 py-1 font-mono text-2xs text-muted">
          staging {batch.data.started.length} · each still needs your OK before submit
          {batch.data.skipped_no_portal ? ` · ${batch.data.skipped_no_portal} without a portal URL` : ""}
        </p>
      ) : null}
      {batch.error ? <p className="border-b border-line px-3 py-1 text-xs text-err">Could not start auto-stage</p> : null}
      <table className="w-full table-fixed border-collapse">
        <thead>
          <tr>
            <th className={cn(th, "w-10")}>#</th>
            <th className={th}>Opportunity</th>
            <th className={cn(th, "w-24")}>Deadline</th>
            <th className={cn(th, "w-16")} />
          </tr>
        </thead>
        <tbody>
          {!queue?.length ? (
            <EmptyRow colSpan={4}>Eligible opportunities are queued here automatically.</EmptyRow>
          ) : (
            queue.map((q) => (
              <tr key={q.application_id} className="hover:bg-surface-2">
                <td className={cn(td, "font-mono text-2xs text-faint tabular")}>{q.priority_rank ?? "—"}</td>
                <td className={cn(td, "py-1")}>
                  <Link href={`/applications/${q.application_id}`} className="block truncate text-xs hover:underline">
                    {q.opportunity.title}
                  </Link>
                  <div className="font-mono text-[10px] text-faint">{q.state}</div>
                </td>
                <td className={td}><DeadlineCell iso={q.deadline_at} /></td>
                <td className={cn(td, "text-right")}>
                  {q.state === "QUEUED" ? (
                    <Button
                      size="sm"
                      onClick={() => start.mutate(q.application_id)}
                      disabled={start.isPending || !q.opportunity.portal_url}
                      title={q.opportunity.portal_url ? "Fill the portal (stops before Submit)" : "No portal URL"}
                    >
                      Fill
                    </Button>
                  ) : null}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </Panel>
  );
}

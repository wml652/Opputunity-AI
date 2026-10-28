"use client";

import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { StatusPill } from "@/components/ui/status";
import { cn, formatTime } from "@/lib/cn";
import { useEventStream } from "@/lib/event-stream";
import { llmStatusPayloadSchema, type PipelineEvent } from "@/lib/schemas";

const COUNTERS = [
  { key: "crawled", label: "Websites Crawled" },
  { key: "found", label: "Opportunities Found" },
  { key: "eligible", label: "Eligible" },
  { key: "needs_input", label: "Needs Input", tone: "warn" as const },
  { key: "ready", label: "Ready for OK", tone: "warn" as const },
  { key: "submitted", label: "Submitted", tone: "ok" as const },
];

const PHASES = [
  "Crawling",
  "Extracting",
  "Evaluating",
  "Preparing",
  "Filling",
  "Awaiting OK",
  "Submitted",
];

function describe(event: PipelineEvent): { summary: string; tone?: "ok" | "err" } {
  if (event.type === "llm.status") {
    const p = llmStatusPayloadSchema.safeParse(event.payload);
    if (p.success) {
      return p.data.ok
        ? { summary: `${p.data.label} · ${p.data.model} · ${p.data.latency_ms} ms`, tone: "ok" }
        : { summary: `${p.data.label} · ${p.data.model} · ${p.data.error_code}`, tone: "err" };
    }
  }
  if (event.type === "llm.config_changed") return { summary: "Provider configuration updated" };
  const p = event.payload as Record<string, unknown>;
  switch (event.type) {
    case "vault.profile_updated":
      return { summary: "Student profile saved (PII encrypted)", tone: "ok" };
    case "vault.document_added":
      return { summary: `Document stored: ${String(p.kind)} · ${String(p.pages)} pages`, tone: "ok" };
    case "vault.document_deleted":
      return { summary: "Document deleted" };
    case "essay.fingerprint_updated":
      return {
        summary: `Fingerprint v${String(p.version)} · ${String(p.essay_count)} essays · burstiness ${Number(p.burstiness).toFixed(3)}`,
        tone: "ok",
      };
    case "memory.learned":
      return { summary: "New answer learned into semantic memory", tone: "ok" };
  }
  return { summary: JSON.stringify(event.payload).slice(0, 140) };
}

export function PipelineOverview() {
  const { events, state, lastSeq } = useEventStream();
  // Campaign counters and phases are fed by discovery/application events from Milestone 3.
  const counts: Record<string, number> = {};

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-6 gap-px overflow-hidden rounded-sm border border-line bg-line">
        {COUNTERS.map((c) => {
          const value = counts[c.key] ?? 0;
          return (
            <div key={c.key} className="flex flex-col gap-1 bg-surface px-3 py-2">
              <span className="text-2xs font-medium uppercase tracking-wide text-faint">
                {c.label}
              </span>
              <span
                className={cn(
                  "font-mono text-xl font-medium tabular",
                  value > 0 && c.tone === "warn" && "text-warn",
                  value > 0 && c.tone === "ok" && "text-ok",
                )}
              >
                {value}
              </span>
            </div>
          );
        })}
      </div>

      <Panel title="Campaign phase" meta="no active campaign">
        <ol className="flex items-center gap-0 px-3 py-2">
          {PHASES.map((phase, i) => (
            <li key={phase} className="flex flex-1 items-center gap-2">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-xs border border-line-strong font-mono text-2xs text-faint">
                {i + 1}
              </span>
              <span className="truncate text-xs text-muted">{phase}</span>
              {i < PHASES.length - 1 ? <span className="h-px flex-1 bg-line" /> : null}
            </li>
          ))}
        </ol>
      </Panel>

      <Panel
        title="Live event log"
        meta={`seq ${lastSeq}`}
        actions={
          <StatusPill tone={state === "live" ? "ok" : "warn"}>
            {state === "live" ? "Streaming" : "Reconnecting"}
          </StatusPill>
        }
      >
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className={cn(th, "w-20")}>Seq</th>
              <th className={cn(th, "w-24")}>Time</th>
              <th className={cn(th, "w-48")}>Type</th>
              <th className={th}>Detail</th>
            </tr>
          </thead>
          <tbody>
            {events.length === 0 ? (
              <EmptyRow colSpan={4}>
                Waiting for events. Verify an LLM provider to emit the first one.
              </EmptyRow>
            ) : (
              events.map((event) => {
                const { summary, tone } = describe(event);
                return (
                  <tr key={event.seq} className="hover:bg-surface-2">
                    <td className={cn(td, "font-mono text-2xs text-faint tabular")}>{event.seq}</td>
                    <td className={cn(td, "font-mono text-2xs text-muted tabular")}>
                      {formatTime(event.created_at)}
                    </td>
                    <td className={cn(td, "font-mono text-2xs")}>{event.type}</td>
                    <td
                      className={cn(
                        td,
                        "truncate text-xs",
                        tone === "ok" && "text-ok",
                        tone === "err" && "text-err",
                      )}
                    >
                      {summary}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

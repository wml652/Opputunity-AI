"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { StatusPill, type Tone } from "@/components/ui/status";
import { cn } from "@/lib/cn";
import { useOpportunities, useOpportunity } from "@/lib/discovery-queries";
import type { Outcome } from "@/lib/discovery-schemas";

import { DeadlineCell, OutcomePill } from "./outcome";

const FILTERS: (Outcome | "ALL")[] = ["ALL", "ELIGIBLE", "UNCERTAIN", "INELIGIBLE", "EXPIRED"];
const RULE_TONE: Record<string, Tone> = { PASS: "ok", FAIL: "err", UNKNOWN: "warn" };

export function OpportunitiesView() {
  const [filter, setFilter] = useState<Outcome | "ALL">("ALL");
  const [selected, setSelected] = useState<string | null>(null);
  const { data, isLoading } = useOpportunities(filter);

  return (
    <>
      <Panel
        title="Opportunities"
        meta={data ? `${data.length} · sorted by deadline urgency` : undefined}
        actions={
          <div className="flex gap-px rounded-sm border border-line bg-line">
            {FILTERS.map((f) => (
              <button
                key={f}
                type="button"
                onClick={() => setFilter(f)}
                className={cn(
                  "h-6 px-2 text-2xs font-medium",
                  filter === f ? "bg-inverse text-inverse-fg" : "bg-surface text-muted hover:text-fg",
                )}
              >
                {f}
              </button>
            ))}
          </div>
        }
      >
        <table className="w-full table-fixed border-collapse">
          <thead>
            <tr>
              <th className={cn(th, "w-28")}>Outcome</th>
              <th className={th}>Opportunity</th>
              <th className={cn(th, "w-28")}>Type</th>
              <th className={cn(th, "w-28")}>Deadline</th>
              <th className={cn(th, "w-40")}>Location</th>
              <th className={cn(th, "w-24 text-right")} />
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <EmptyRow colSpan={6}>Loading</EmptyRow>
            ) : !data?.length ? (
              <EmptyRow colSpan={6}>No opportunities{filter === "ALL" ? " yet. Launch a campaign from the Pipeline page." : ` with outcome ${filter}.`}</EmptyRow>
            ) : (
              data.map((o) => (
                <tr key={o.id} onClick={() => setSelected(o.id)} className="cursor-pointer hover:bg-surface-2">
                  <td className={td}><OutcomePill outcome={o.latest_outcome} /></td>
                  <td className={cn(td, "py-1")}>
                    <div className="truncate text-xs font-medium">{o.title}</div>
                    <div className="truncate text-2xs text-faint">{o.organization ?? "unknown organization"}</div>
                  </td>
                  <td className={cn(td, "font-mono text-2xs text-muted")}>{o.type}</td>
                  <td className={td}><DeadlineCell iso={o.deadline_at} /></td>
                  <td className={cn(td, "truncate text-2xs text-muted")}>{o.location ?? (o.is_international ? "International" : "—")}</td>
                  <td className={cn(td, "text-right")}>
                    <Button size="sm" variant="ghost">Evidence</Button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </Panel>
      <OpportunityDrawer id={selected} onClose={() => setSelected(null)} />
    </>
  );
}

function OpportunityDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { data } = useOpportunity(id);
  const o = data?.opportunity;
  const v = data?.verdict;
  const rules = o?.eligibility_rules as Record<string, unknown> | undefined;
  return (
    <Dialog.Root open={id !== null} onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30" />
        <Dialog.Content className="fixed inset-y-0 right-0 z-50 flex w-[560px] max-w-full flex-col border-l border-line-strong bg-surface shadow-lg focus:outline-none">
          <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
            <div className="min-w-0">
              <Dialog.Title className="text-sm font-semibold">{o?.title ?? "Loading"}</Dialog.Title>
              <Dialog.Description className="text-2xs text-faint">
                {o ? `${o.organization ?? "unknown organization"} · ${o.type}` : ""}
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <Button size="sm" variant="ghost">Close</Button>
            </Dialog.Close>
          </header>
          {o ? (
            <div className="flex-1 overflow-y-auto">
              <div className="grid grid-cols-3 gap-px border-b border-line bg-line">
                <div className="bg-surface px-3 py-2">
                  <div className="text-[10px] uppercase tracking-wide text-faint">Outcome</div>
                  <OutcomePill outcome={o.latest_outcome} />
                </div>
                <div className="bg-surface px-3 py-2">
                  <div className="text-[10px] uppercase tracking-wide text-faint">Deadline</div>
                  <DeadlineCell iso={o.deadline_at} />
                </div>
                <div className="bg-surface px-3 py-2">
                  <div className="text-[10px] uppercase tracking-wide text-faint">AI policy</div>
                  <span className={cn("font-mono text-2xs", o.ai_policy === "PROHIBITED" && "text-warn")}>{o.ai_policy}</span>
                </div>
              </div>
              <section className="border-b border-line px-4 py-3">
                <p className="text-xs text-muted">{o.summary || "No summary extracted."}</p>
                <div className="mt-2 flex flex-col gap-1 font-mono text-2xs">
                  {o.portal_url ? (
                    <a href={o.portal_url} target="_blank" rel="noopener noreferrer nofollow" className="truncate text-fg underline underline-offset-2">
                      Portal: {o.portal_url}
                    </a>
                  ) : null}
                  {o.source_url ? <span className="truncate text-faint">Source: {o.source_url}</span> : null}
                  {o.deadline_raw ? <span className="text-faint">Deadline as written: {o.deadline_raw}</span> : null}
                </div>
              </section>
              <section className="border-b border-line px-4 py-3">
                <h3 className="mb-2 text-2xs font-semibold uppercase tracking-wide text-muted">Eligibility evidence</h3>
                {!v ? (
                  <p className="text-xs text-faint">Not evaluated yet.</p>
                ) : (
                  <ul className="flex flex-col divide-y divide-line">
                    {v.reasons.map((r, i) => (
                      <li key={`${r.rule}-${i}`} className="flex items-start gap-2 py-1.5">
                        <StatusPill tone={RULE_TONE[r.status] ?? "idle"} className="shrink-0">{r.status}</StatusPill>
                        <div className="min-w-0">
                          <div className="text-xs font-medium">{r.rule}</div>
                          <div className="text-2xs text-muted">{r.evidence}</div>
                        </div>
                        <span className="ml-auto shrink-0 font-mono text-[10px] text-faint">{r.source === "LLM" ? "LLM" : "rule"}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {v ? (
                  <p className="mt-2 font-mono text-[10px] text-faint">
                    {v.model ? `stage 2 model: ${v.model}` : "decided by deterministic rules (no LLM)"} ·{" "}
                    {new Date(v.evaluated_at).toLocaleString()}
                  </p>
                ) : null}
              </section>
              <section className="px-4 py-3">
                <h3 className="mb-2 text-2xs font-semibold uppercase tracking-wide text-muted">Extracted criteria</h3>
                <pre className="whitespace-pre-wrap rounded-sm bg-surface-2 p-2 font-mono text-[10px] text-muted">
                  {JSON.stringify(rules, null, 2)}
                </pre>
              </section>
            </div>
          ) : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

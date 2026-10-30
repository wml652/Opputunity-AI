"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { StatusPill, type Tone } from "@/components/ui/status";
import { cn, formatRelative } from "@/lib/cn";
import { useCampaign, useCampaigns, useCancelCampaign } from "@/lib/discovery-queries";
import type { CampaignT } from "@/lib/discovery-schemas";

import { CampaignLauncher } from "./campaign-launcher";
import { CrawlInspector } from "./crawl-inspector";

const STATUS_TONE: Record<CampaignT["status"], Tone> = {
  DRAFT: "idle",
  RUNNING: "warn",
  PAUSED: "idle",
  COMPLETED: "ok",
  FAILED: "err",
  CANCELLED: "idle",
};

const PAGE_TONE: Record<string, Tone> = {
  DISTILLED: "warn",
  EXTRACTED: "ok",
  BLOCKED_SSRF: "err",
  FAILED: "err",
  SKIPPED: "idle",
  BLOCKED_BOT_CHALLENGE: "warn",
};

export function CampaignsView() {
  const { data: campaigns, isLoading } = useCampaigns();
  const [selected, setSelected] = useState<string | null>(null);
  const active = selected ?? campaigns?.[0]?.id ?? null;

  return (
    <div className="flex flex-col gap-3">
      <CampaignLauncher onLaunched={setSelected} />
      <div className="grid grid-cols-5 gap-3">
        <Panel title="Campaigns" meta={campaigns ? `${campaigns.length}` : undefined} className="col-span-2">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className={th}>Campaign</th>
                <th className={th}>Status</th>
                <th className={cn(th, "text-right")}>Found</th>
                <th className={cn(th, "text-right")}>Eligible</th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <EmptyRow colSpan={4}>Loading</EmptyRow>
              ) : !campaigns?.length ? (
                <EmptyRow colSpan={4}>No campaigns yet.</EmptyRow>
              ) : (
                campaigns.map((c) => (
                  <tr
                    key={c.id}
                    onClick={() => setSelected(c.id)}
                    className={cn("cursor-pointer hover:bg-surface-2", active === c.id && "bg-surface-2")}
                  >
                    <td className={td}>
                      <div className="truncate text-xs font-medium">{c.name}</div>
                      <div className="font-mono text-[10px] text-faint">
                        {c.scope} · {formatRelative(c.created_at)}
                      </div>
                    </td>
                    <td className={td}>
                      <StatusPill tone={STATUS_TONE[c.status]}>
                        {c.status === "RUNNING" ? c.phase : c.status}
                      </StatusPill>
                    </td>
                    <td className={cn(td, "text-right font-mono text-2xs tabular")}>{c.counters.opportunities_found ?? 0}</td>
                    <td className={cn(td, "text-right font-mono text-2xs tabular")}>{c.counters.eligible ?? 0}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </Panel>
        <CampaignDetail id={active} />
      </div>
    </div>
  );
}

function CampaignDetail({ id }: { id: string | null }) {
  const { data } = useCampaign(id);
  const cancel = useCancelCampaign();
  const [inspecting, setInspecting] = useState<string | null>(null);
  if (!data) {
    return (
      <Panel title="Campaign detail" className="col-span-3">
        <p className="p-3 text-xs text-faint">{id ? "Loading" : "Launch a campaign to see its crawl log."}</p>
      </Panel>
    );
  }
  const { campaign: c, pages } = data;
  const counters = c.counters;
  const stats: [string, number][] = [
    ["crawled", counters.websites_crawled ?? 0],
    ["blocked", counters.pages_blocked ?? 0],
    ["failed", counters.pages_failed ?? 0],
    ["skipped", counters.pages_skipped ?? 0],
    ["adapted", counters.adapted ?? 0],
    ["challenges", counters.bot_challenges ?? 0],
    ["hubs", counters.hubs_harvested ?? 0],
    ["details", counters.details_queued ?? 0],
    ["found", counters.opportunities_found ?? 0],
    ["dupes", counters.duplicates ?? 0],
    ["eligible", counters.eligible ?? 0],
    ["uncertain", counters.uncertain ?? 0],
    ["ineligible", counters.ineligible ?? 0],
    ["expired", counters.expired ?? 0],
    ["queued", counters.queued ?? 0],
  ];
  return (
    <Panel
      title={c.name}
      meta={`${c.keywords.join(", ")} · ${c.sources.length} sources`}
      className="col-span-3"
      actions={
        c.status === "RUNNING" || c.status === "DRAFT" ? (
          <Button size="sm" variant="danger" onClick={() => cancel.mutate(c.id)} disabled={cancel.isPending}>
            Cancel
          </Button>
        ) : null
      }
    >
      <div className="grid grid-cols-5 gap-px border-b border-line bg-line">
        {stats.map(([label, value]) => (
          <div key={label} className="bg-surface px-2 py-1.5">
            <div className="text-[10px] uppercase tracking-wide text-faint">{label}</div>
            <div className="font-mono text-sm tabular">{value}</div>
          </div>
        ))}
      </div>
      {c.error ? <p className="border-b border-line bg-err-soft px-3 py-1.5 text-xs text-err">{c.error}</p> : null}
      <div className="max-h-[440px] overflow-auto">
        <table className="w-full table-fixed border-collapse">
          <thead className="sticky top-0 bg-surface">
            <tr>
              <th className={cn(th, "w-28")}>Status</th>
              <th className={th}>Page</th>
              <th className={cn(th, "w-20 text-right")}>Chars</th>
              <th className={cn(th, "w-24")}>Flags</th>
            </tr>
          </thead>
          <tbody>
            {pages.length === 0 ? (
              <EmptyRow colSpan={4}>{c.status === "RUNNING" ? "Crawling" : "No pages."}</EmptyRow>
            ) : (
              pages.map((p) => (
                <tr
                  key={p.id}
                  onClick={() => setInspecting(p.id)}
                  className="cursor-pointer hover:bg-surface-2"
                  title="Open the crawl inspector"
                >
                  <td className={td}>
                    <StatusPill tone={PAGE_TONE[p.status] ?? "idle"}>{p.status}</StatusPill>
                  </td>
                  <td className={cn(td, "py-1")}>
                    <div className="truncate text-xs" title={p.title ?? undefined}>{p.title ?? p.url}</div>
                    <div className="truncate font-mono text-[10px] text-faint" title={p.final_url ?? p.url}>
                      {p.final_url ?? p.url}
                      {p.error_stage ? ` · ${p.error_stage}` : ""}
                      {p.error ? ` · ${p.error}` : ""}
                    </div>
                  </td>
                  <td className={cn(td, "text-right font-mono text-2xs tabular")}>{p.markdown_chars.toLocaleString()}</td>
                  <td className={td}>
                    {p.injection_flags.length ? (
                      <span
                        className="font-mono text-2xs text-warn"
                        title={p.injection_flags.map((f) => `${f.pattern}: ${f.excerpt}`).join("\n")}
                      >
                        {p.injection_flags.length} injection
                      </span>
                    ) : (
                      <span className="text-2xs text-faint">clean</span>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <CrawlInspector campaignId={c.id} pageId={inspecting} onClose={() => setInspecting(null)} />
    </Panel>
  );
}

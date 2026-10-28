"use client";

import * as Dialog from "@radix-ui/react-dialog";

import { Button } from "@/components/ui/button";
import { StatusPill, type Tone } from "@/components/ui/status";
import { cn } from "@/lib/cn";
import { usePageDetail, useRetryPage } from "@/lib/m4-queries";
import type { TraceStep } from "@/lib/m4-schemas";

const STEP_TONE: Record<string, Tone> = { ok: "ok", fail: "err", skip: "idle" };

function fmtDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

function DetailValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-faint">—</span>;
  if (typeof value === "object") return <span>{JSON.stringify(value)}</span>;
  return <span>{String(value)}</span>;
}

function TimelineStep({ step, last }: { step: TraceStep; last: boolean }) {
  const entries = Object.entries(step.detail ?? {});
  return (
    <li className="relative flex gap-3 pb-3">
      {!last ? <span className="absolute left-[5px] top-4 h-full w-px bg-line" aria-hidden="true" /> : null}
      <span
        className={cn(
          "mt-1.5 h-[11px] w-[11px] shrink-0 rounded-full border-2 bg-surface",
          step.status === "fail" ? "border-err" : step.status === "ok" ? "border-ok" : "border-line-strong",
        )}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-mono text-2xs font-semibold">{step.stage}</span>
          <StatusPill tone={STEP_TONE[step.status] ?? "idle"}>{step.status}</StatusPill>
          <span className="ml-auto font-mono text-[10px] text-faint">{fmtDuration(step.duration_ms)}</span>
        </div>
        {entries.length ? (
          <dl className="mt-1 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 font-mono text-[10px]">
            {entries.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-faint">{k}</dt>
                <dd className="break-all text-muted">
                  <DetailValue value={v} />
                </dd>
              </div>
            ))}
          </dl>
        ) : null}
      </div>
    </li>
  );
}

/** Flight-recorder drawer for one crawled page: discovery, preflight, fetch, distill, extraction. */
export function CrawlInspector({
  campaignId,
  pageId,
  onClose,
}: {
  campaignId: string;
  pageId: string | null;
  onClose: () => void;
}) {
  const { data: p, isLoading } = usePageDetail(campaignId, pageId);
  const retry = useRetryPage();
  const failed = p ? ["FAILED", "BLOCKED_SSRF", "SKIPPED", "DISTILLED"].includes(p.status) : false;
  return (
    <Dialog.Root open={pageId !== null} onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30" />
        <Dialog.Content className="fixed inset-y-0 right-0 z-50 flex w-[640px] max-w-full flex-col border-l border-line-strong bg-surface shadow-lg focus:outline-none">
          <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
            <div className="min-w-0">
              <Dialog.Title className="truncate text-sm font-semibold">
                {p ? (p.title ?? p.url) : "Crawl inspector"}
              </Dialog.Title>
              <Dialog.Description className="truncate font-mono text-2xs text-faint">
                {p ? (p.final_url ?? p.url) : isLoading ? "Loading" : ""}
              </Dialog.Description>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              {p ? (
                <Button
                  size="sm"
                  variant={failed ? "primary" : "secondary"}
                  disabled={retry.isPending || retry.isSuccess}
                  onClick={() => retry.mutate({ campaignId, pageId: p.id })}
                  title="Re-run this page with an escalated fetch and extraction strategy"
                >
                  {retry.isSuccess ? "Retry queued" : "Retry Page with Adaptive Strategy"}
                </Button>
              ) : null}
              <Dialog.Close asChild>
                <Button size="sm" variant="ghost">Close</Button>
              </Dialog.Close>
            </div>
          </header>
          {retry.error ? <p className="border-b border-line bg-err-soft px-4 py-1.5 text-xs text-err">{retry.error.message}</p> : null}
          {p ? (
            <div className="flex-1 overflow-y-auto">
              <div className="grid grid-cols-4 gap-px border-b border-line bg-line">
                {[
                  ["Status", p.status],
                  ["HTTP", p.http_status ?? "—"],
                  ["Chars", p.markdown_chars.toLocaleString()],
                  ["Failed at", p.error_stage ?? "—"],
                ].map(([label, value]) => (
                  <div key={String(label)} className="bg-surface px-3 py-2">
                    <div className="text-[10px] uppercase tracking-wide text-faint">{label}</div>
                    <div className={cn("font-mono text-xs", label === "Failed at" && p.error_stage && "text-err")}>
                      {String(value)}
                    </div>
                  </div>
                ))}
              </div>
              <section className="border-b border-line px-4 py-3 text-xs">
                <h3 className="mb-1.5 text-2xs font-semibold uppercase tracking-wide text-muted">Discovery</h3>
                <div className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 font-mono text-2xs">
                  <span className="text-faint">source</span>
                  <span>{p.discovery_source ?? "—"}</span>
                  <span className="text-faint">query</span>
                  <span className="break-all">{p.discovery_query ?? "—"}</span>
                  {p.log_file ? (
                    <>
                      <span className="text-faint">log</span>
                      <span className="break-all text-muted">{p.log_file}</span>
                    </>
                  ) : null}
                </div>
                {p.error || p.error_detail ? (
                  <div className="mt-2 rounded-sm border border-err/30 bg-err-soft px-2 py-1.5 text-2xs text-err">
                    <div className="font-mono">{p.error_code ?? "error"}: {p.error}</div>
                    {p.error_detail ? <div className="mt-0.5 break-all text-muted">{p.error_detail}</div> : null}
                  </div>
                ) : null}
              </section>
              <section className="border-b border-line px-4 py-3">
                <h3 className="mb-2 text-2xs font-semibold uppercase tracking-wide text-muted">Timeline</h3>
                {p.trace.length === 0 ? (
                  <p className="text-xs text-faint">No trace recorded for this page.</p>
                ) : (
                  <ol>
                    {p.trace.map((step, i) => (
                      <TimelineStep key={`${step.stage}-${i}`} step={step} last={i === p.trace.length - 1} />
                    ))}
                  </ol>
                )}
              </section>
              {Object.keys(p.strategy).length ? (
                <section className="border-b border-line px-4 py-3">
                  <h3 className="mb-1.5 text-2xs font-semibold uppercase tracking-wide text-muted">Strategy used</h3>
                  <pre className="whitespace-pre-wrap rounded-sm bg-surface-2 p-2 font-mono text-[10px] text-muted">
                    {JSON.stringify(p.strategy, null, 2)}
                  </pre>
                </section>
              ) : null}
              <section className="px-4 py-3">
                <h3 className="mb-1.5 text-2xs font-semibold uppercase tracking-wide text-muted">
                  Distilled Markdown {p.markdown_chars > 8000 ? "(first 8,000 chars)" : ""}
                </h3>
                {p.markdown_preview ? (
                  <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap rounded-sm bg-surface-2 p-2 font-mono text-[10px] text-muted">
                    {p.markdown_preview}
                  </pre>
                ) : (
                  <p className="text-xs text-faint">Nothing was distilled from this page.</p>
                )}
              </section>
            </div>
          ) : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

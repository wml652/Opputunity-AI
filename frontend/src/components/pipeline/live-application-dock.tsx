"use client";

import * as Dialog from "@radix-ui/react-dialog";
import Link from "next/link";
import { useMemo, useState } from "react";

import { UnblockButton } from "@/components/applications/unblock-button";
import { VerificationDrawer } from "@/components/inbox/verification-drawer";
import { Button } from "@/components/ui/button";
import { StatusPill, type Tone } from "@/components/ui/status";
import { cn } from "@/lib/cn";
import { useEventStream, type LiveStep } from "@/lib/event-stream";

const ACTIVE = new Set(["PREPARING", "FILLING", "RESUME_QUEUED", "SUBMITTING", "SUBMIT_APPROVED"]);
const RECENT_MS = 30 * 60 * 1000;
const MAX_WINDOWS = 6;
const TONE: Record<string, Tone> = {
  AWAITING_FINAL_VERIFICATION: "ok",
  SUBMITTED: "ok",
  PAUSED_NEEDS_USER_INPUT: "warn",
  BLOCKED_NEEDS_HUMAN: "warn",
  SUBMISSION_FAILED: "err",
  FAILED: "err",
};

function host(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

interface Window {
  step: LiveStep;
  state: string;
}

/**
 * Live Application Theatre: a collapsible bottom dock with one mini browser window per running
 * (or recently finished) fill / submit - the live frame, the current hop / step / field, a fill
 * bar, and the next action (verify, answer, sign in).
 */
export function LiveApplicationDock() {
  const { liveSteps, events, learned } = useEventStream();
  const [open, setOpen] = useState(true);
  const [zoom, setZoom] = useState<LiveStep | null>(null);
  const [verifying, setVerifying] = useState<string | null>(null);

  const windows = useMemo<Window[]>(() => {
    // The latest application.state per application (events arrive newest first).
    const states = new Map<string, string>();
    for (const e of events) {
      if (e.type !== "application.state") continue;
      const id = String(e.payload.application_id ?? "");
      if (id && !states.has(id)) states.set(id, String(e.payload.state ?? ""));
    }
    const now = Date.now();
    return Object.values(liveSteps)
      .map((step) => ({ step, state: states.get(step.application_id) ?? step.state }))
      .filter(({ step, state }) => ACTIVE.has(state) || now - Date.parse(step.updated_at) < RECENT_MS)
      .sort((a, b) => Date.parse(b.step.updated_at) - Date.parse(a.step.updated_at))
      .slice(0, MAX_WINDOWS);
  }, [liveSteps, events]);

  const running = windows.filter((w) => ACTIVE.has(w.state)).length;
  const teaching = Object.values(learned)
    .map((steps) => steps[steps.length - 1])
    .filter((s): s is NonNullable<typeof s> => !!s);
  const recentLearned = Object.values(learned).flat().slice(-8).reverse();
  if (windows.length === 0 && teaching.length === 0) return null;

  return (
    <div className="sticky bottom-0 z-20 -mx-4 mt-3 border-t border-line-strong bg-surface">
      <div className="flex items-center gap-2 px-4 py-1.5">
        <span className="text-2xs font-semibold uppercase tracking-wide text-muted">Live application theatre</span>
        <span className="font-mono text-2xs text-faint tabular">
          {running} running · {windows.length - running} recent
        </span>
        <Button size="sm" variant="ghost" className="ml-auto" onClick={() => setOpen((v) => !v)}>
          {open ? "Collapse" : "Expand"}
        </Button>
      </div>
      {open && recentLearned.length ? (
        <div className="mx-4 mb-2 rounded-sm border border-line bg-surface-2">
          <div className="border-b border-line px-2 py-1 text-2xs font-semibold uppercase tracking-wide text-muted">
            Teach Mode - the agent is learning ({teaching.map((t) => t.title).join(", ")})
          </div>
          <ol className="max-h-36 overflow-y-auto">
            {recentLearned.map((s) => (
              <li key={`${s.application_id}-${s.n}`} className="flex gap-2 border-b border-line px-2 py-0.5 last:border-b-0">
                <span className="w-8 shrink-0 font-mono text-[10px] text-faint tabular">#{s.n}</span>
                <span className="w-16 shrink-0 font-mono text-[10px] text-muted">{s.kind}</span>
                <span className="truncate text-2xs" title={s.label}>{s.label || host(s.url)}</span>
                {s.value_preview ? <span className="ml-auto truncate font-mono text-[10px] text-faint">{s.value_preview}</span> : null}
              </li>
            ))}
          </ol>
        </div>
      ) : null}
      {open ? (
        <div className="flex gap-2 overflow-x-auto px-4 pb-3">
          {windows.map(({ step, state }) => (
            <div key={step.application_id} className="flex w-72 shrink-0 flex-col overflow-hidden rounded-sm border border-line bg-surface-2">
              <button
                type="button"
                className="relative aspect-video w-full bg-surface-3"
                onClick={() => setZoom(step)}
                title="Click to zoom"
              >
                {step.frame_seq > 0 ? (
                  // eslint-disable-next-line @next/next/no-img-element -- authenticated live API frame
                  <img
                    src={`/api/v1/applications/${step.application_id}/live-frame?t=${step.frame_seq}`}
                    alt={`Live view of ${step.opportunity_title}`}
                    className="h-full w-full object-cover object-top"
                  />
                ) : (
                  <span className="font-mono text-2xs text-faint">waiting for the first frame</span>
                )}
              </button>
              <div className="flex flex-col gap-1 p-2">
                <div className="flex items-center gap-1">
                  <span className="truncate text-xs font-medium" title={step.opportunity_title}>
                    {step.opportunity_title}
                  </span>
                  <StatusPill tone={TONE[state] ?? (ACTIVE.has(state) ? "warn" : "idle")}>{state.replaceAll("_", " ").toLowerCase()}</StatusPill>
                </div>
                <div className="truncate font-mono text-[10px] text-faint" title={step.current_url}>
                  {host(step.current_url)}
                </div>
                <div className="truncate text-2xs text-muted" title={step.phase_label}>
                  {step.phase_label}
                  {step.value_source ? <span className="ml-1 font-mono text-faint">[{step.value_source}]</span> : null}
                </div>
                <div className="flex items-center gap-2">
                  <div className="h-1 flex-1 overflow-hidden rounded-xs bg-surface-3">
                    <div
                      className={cn("h-full transition-[width] duration-500", step.final ? "bg-ok" : "bg-fg")}
                      style={{ width: `${Math.min(100, step.progress_pct)}%` }}
                    />
                  </div>
                  <span className="w-10 text-right font-mono text-[10px] tabular">{step.progress_pct.toFixed(0)}%</span>
                </div>
                <div className="flex flex-wrap gap-1">
                  {state === "AWAITING_FINAL_VERIFICATION" ? (
                    <Button size="sm" variant="primary" onClick={() => setVerifying(step.application_id)}>
                      OK — Verify &amp; Submit
                    </Button>
                  ) : null}
                  {state === "PAUSED_NEEDS_USER_INPUT" ? (
                    <Button size="sm" asChild>
                      <Link href="/inbox">Answer question</Link>
                    </Button>
                  ) : null}
                  {state === "BLOCKED_NEEDS_HUMAN" ? <UnblockButton applicationId={step.application_id} variant="secondary" /> : null}
                  <Button size="sm" variant="ghost" asChild>
                    <Link href={`/applications/${step.application_id}`}>Details</Link>
                  </Button>
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : null}
      <Dialog.Root open={zoom !== null} onOpenChange={(o) => !o && setZoom(null)}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-40 bg-black/50" />
          <Dialog.Content className="fixed inset-8 z-50 flex flex-col overflow-hidden rounded-sm border border-line-strong bg-surface">
            <div className="flex items-center gap-2 border-b border-line px-3 py-2">
              <Dialog.Title className="truncate text-sm font-medium">{zoom?.opportunity_title}</Dialog.Title>
              <span className="truncate font-mono text-2xs text-faint">{zoom ? zoom.current_url : ""}</span>
              <Dialog.Close asChild>
                <Button size="sm" variant="ghost" className="ml-auto">Close</Button>
              </Dialog.Close>
            </div>
            {zoom ? (
              // eslint-disable-next-line @next/next/no-img-element -- authenticated live API frame
              <img
                src={`/api/v1/applications/${zoom.application_id}/live-frame?t=${liveSteps[zoom.application_id]?.frame_seq ?? zoom.frame_seq}`}
                alt="Live portal view"
                className="min-h-0 flex-1 object-contain"
              />
            ) : null}
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      <VerificationDrawer id={verifying} onClose={() => setVerifying(null)} />
    </div>
  );
}

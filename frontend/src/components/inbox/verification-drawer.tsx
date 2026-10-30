"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useMemo, useState } from "react";

import { EssayScores } from "@/components/vault/essay-synthesizer";
import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/field";
import { StatusPill } from "@/components/ui/status";
import { cn } from "@/lib/cn";
import { useApprove, useReject, useVerification } from "@/lib/m4-queries";
import type { FieldT, VerificationT } from "@/lib/m4-schemas";

const SOURCE_STYLE: Record<string, string> = {
  PROFILE: "text-fg border-line-strong",
  DOCUMENT: "text-fg border-line-strong",
  ESSAY: "text-fg border-line-strong",
  MEMORY: "text-ok border-ok/40",
  USER: "text-ok border-ok/40",
  NONE: "text-warn border-warn/40",
};

const ZOOMS = [50, 75, 100, 150, 200];

/** Final verification: what will be submitted, exactly. Nothing is sent without this OK, and the
 * OK is bound to the preview hash shown here (a changed application is rejected as stale). */
export function VerificationDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { data, isLoading, error } = useVerification(id);
  return (
    <Dialog.Root open={id !== null} onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30" />
        <Dialog.Content className="fixed inset-y-0 right-0 z-50 flex w-[1180px] max-w-full flex-col border-l border-line-strong bg-surface shadow-lg focus:outline-none">
          <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
            <div className="min-w-0">
              <Dialog.Title className="truncate text-sm font-semibold">
                {data ? `Verify & submit: ${data.opportunity.title}` : "Final verification"}
              </Dialog.Title>
              <Dialog.Description className="truncate font-mono text-2xs text-faint">
                {data ? data.opportunity.portal_url : isLoading ? "Loading" : ""}
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <Button size="sm" variant="ghost">Close</Button>
            </Dialog.Close>
          </header>
          {error ? <p className="p-4 text-xs text-err">{error.message}</p> : null}
          {data ? <Body key={data.preview_hash} data={data} onDone={onClose} /> : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Body({ data, onDone }: { data: VerificationT; onDone: () => void }) {
  const approve = useApprove();
  const reject = useReject();
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [zoom, setZoom] = useState(75);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const state = data.application.state;
  const awaiting = state === "AWAITING_FINAL_VERIFICATION";
  const steps = useMemo(() => {
    const by = new Map<number, FieldT[]>();
    for (const f of data.fields) by.set(f.step_index, [...(by.get(f.step_index) ?? []), f]);
    return [...by.entries()].sort((a, b) => a[0] - b[0]);
  }, [data.fields]);
  const changed = Object.entries(edits).filter(
    ([k, v]) => v !== (data.fields.find((f) => f.field_key === k)?.value ?? ""),
  );
  const manualDraft = data.drafts.find(
    (d) =>
      d.requires_manual &&
      d.field_key &&
      !edits[d.field_key] &&
      !data.fields.find((f) => f.field_key === d.field_key)?.user_edited,
  );
  const latest = data.latest;
  return (
    <>
      {latest && latest.outcome === "RESTAGED" && awaiting ? (
        <div className="border-b border-line bg-warn-soft px-4 py-2 text-xs text-warn">
          Not submitted: the portal showed different values than you approved (
          {latest.mismatches.map((m) => m.field).join(", ") || latest.error}). Review and approve again.
        </div>
      ) : null}
      {state === "SUBMITTED" && latest ? <Receipt id={data.application.id} data={data} /> : null}
      <div className="grid min-h-0 flex-1 grid-cols-2">
        <section className="flex min-h-0 flex-col border-r border-line">
          <div className="flex h-8 items-center justify-between border-b border-line px-3">
            <span className="text-2xs font-semibold uppercase tracking-wide text-muted">
              Portal at the final step
            </span>
            <div className="flex items-center gap-1">
              {ZOOMS.map((z) => (
                <button
                  key={z}
                  onClick={() => setZoom(z)}
                  className={cn(
                    "h-5 rounded-xs border px-1.5 font-mono text-[10px]",
                    zoom === z ? "border-line-strong bg-surface-3 text-fg" : "border-line text-faint hover:text-fg",
                  )}
                >
                  {z}%
                </button>
              ))}
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-auto bg-surface-2 p-3">
            {data.checkpoint?.has_screenshot ? (
              // eslint-disable-next-line @next/next/no-img-element -- authenticated API image
              <img
                src={`/api/v1/applications/checkpoints/${data.checkpoint.id}/screenshot`}
                alt="Full-page portal screenshot before submission"
                style={{ width: `${zoom}%`, maxWidth: "none" }}
                className="border border-line"
              />
            ) : (
              <p className="text-xs text-faint">No screenshot was captured.</p>
            )}
          </div>
        </section>
        <section className="flex min-h-0 flex-col">
          <div className="min-h-0 flex-1 overflow-y-auto">
            {steps.map(([step, fields]) => (
              <div key={step} className="border-b border-line">
                <div className="bg-surface-2 px-3 py-1 text-2xs font-semibold uppercase tracking-wide text-muted">
                  Step {step + 1}
                </div>
                {fields.map((f) => (
                  <FieldRow
                    key={f.field_key}
                    f={f}
                    value={edits[f.field_key]}
                    disabled={!awaiting}
                    onChange={(v) => setEdits({ ...edits, [f.field_key]: v })}
                  />
                ))}
              </div>
            ))}
            {data.drafts.length ? (
              <div className="border-b border-line px-3 py-2">
                <div className="mb-1 text-2xs font-semibold uppercase tracking-wide text-muted">Essays</div>
                {data.drafts.map((d) => (
                  <div key={d.id} className="flex items-center justify-between gap-2 py-1">
                    <span className="truncate text-xs">{d.field_key ?? d.prompt}</span>
                    <EssayScores draft={d} />
                  </div>
                ))}
              </div>
            ) : null}
          </div>
          <footer className="flex flex-col gap-2 border-t border-line px-3 py-2">
            <div className="flex items-center justify-between font-mono text-[10px] text-faint">
              <span title={data.preview_hash}>preview {data.preview_hash.slice(0, 16)}…</span>
              <span>{changed.length ? `${changed.length} edit(s) will be applied and verified on the page` : "no edits"}</span>
            </div>
            {manualDraft ? (
              <p className="text-2xs text-warn">
                This portal prohibits AI-written essays: rewrite “{manualDraft.field_key}” yourself before approving.
              </p>
            ) : null}
            {approve.error ? <p className="text-2xs text-err">{approve.error.message}</p> : null}
            {reject.error ? <p className="text-2xs text-err">{reject.error.message}</p> : null}
            {awaiting ? (
              rejecting ? (
                <div className="flex items-center gap-2">
                  <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason (optional)" />
                  <Button size="sm" variant="ghost" onClick={() => setRejecting(false)}>Back</Button>
                  <Button
                    size="sm"
                    variant="danger"
                    disabled={reject.isPending}
                    onClick={() =>
                      reject.mutate(
                        { id: data.application.id, preview_hash: data.preview_hash, reason },
                        { onSuccess: onDone },
                      )
                    }
                  >
                    Confirm reject
                  </Button>
                </div>
              ) : (
                <div className="flex items-center justify-end gap-2">
                  <Button variant="danger" onClick={() => setRejecting(true)}>Reject / Cancel</Button>
                  <Button
                    variant="primary"
                    disabled={approve.isPending || Boolean(manualDraft)}
                    onClick={() =>
                      approve.mutate({
                        id: data.application.id,
                        preview_hash: data.preview_hash,
                        edits: Object.fromEntries(changed),
                      })
                    }
                  >
                    {approve.isPending ? "Recording your OK" : "OK — Verify & Submit"}
                  </Button>
                </div>
              )
            ) : (
              <div className="flex items-center gap-2 text-xs">
                <StatusPill tone={state === "SUBMITTED" ? "ok" : state === "SUBMISSION_FAILED" ? "err" : "warn"}>
                  {state}
                </StatusPill>
                {state === "SUBMIT_APPROVED" || state === "SUBMITTING" ? (
                  <span className="text-muted">Re-filling every step with your approved values, checking each one on the page…</span>
                ) : null}
                {latest?.error && state === "SUBMISSION_FAILED" ? <span className="text-err">{latest.error}</span> : null}
              </div>
            )}
          </footer>
        </section>
      </div>
    </>
  );
}

function FieldRow({
  f,
  value,
  disabled,
  onChange,
}: {
  f: FieldT;
  value: string | undefined;
  disabled: boolean;
  onChange: (v: string) => void;
}) {
  const original = f.value ?? "";
  const current = value ?? original;
  const edited = value !== undefined && value !== original;
  const id = `vf-${f.field_key}`;
  const long = f.field_type === "textarea";
  return (
    <div className={cn("flex flex-col gap-1 px-3 py-2", edited && "bg-warn-soft")}>
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={id} className="truncate text-xs font-medium" title={f.label}>
          {f.label}
          {f.required ? <span className="text-faint"> *</span> : null}
        </label>
        <span
          className={cn(
            "inline-flex h-5 shrink-0 items-center rounded-xs border px-1.5 font-mono text-[10px]",
            edited ? SOURCE_STYLE.USER : (SOURCE_STYLE[f.source] ?? SOURCE_STYLE.NONE),
          )}
        >
          {edited ? "USER (edited)" : f.user_edited ? "USER" : f.source}
        </span>
      </div>
      {f.field_type === "file" ? (
        <span className="font-mono text-2xs text-muted">{original || "no document"} (attached from your Vault)</span>
      ) : f.options.length ? (
        <select
          id={id}
          value={current}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          className="h-7 rounded-sm border border-line-strong bg-surface px-2 text-sm"
        >
          {!f.options.includes(current) ? <option value={current}>{current || "(blank)"}</option> : null}
          {f.options.map((o) => (
            <option key={o} value={o}>{o}</option>
          ))}
        </select>
      ) : long ? (
        <Textarea id={id} rows={6} value={current} disabled={disabled} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <Input id={id} value={current} disabled={disabled} onChange={(e) => onChange(e.target.value)} />
      )}
      {f.evidence ? (
        <span className="line-clamp-3 font-mono text-[10px] text-faint" title={f.evidence}>
          grounded in {f.source.toLowerCase()}: {f.evidence}
        </span>
      ) : null}
      {edited ? (
        <span className="font-mono text-[10px] text-faint">
          was: <span className="line-through">{original.slice(0, 160) || "(blank)"}</span>
        </span>
      ) : null}
    </div>
  );
}

function Receipt({ id, data }: { id: string; data: VerificationT }) {
  const latest = data.latest;
  if (!latest) return null;
  return (
    <div className="flex gap-3 border-b border-line bg-ok-soft px-4 py-2">
      <div className="min-w-0 flex-1">
        <div className="text-xs font-medium text-ok">
          Submitted {latest.submitted_at ? new Date(latest.submitted_at).toLocaleString() : ""}
        </div>
        <p className="line-clamp-3 text-2xs text-muted">{data.receipt_text || "No confirmation text captured."}</p>
        <div className="font-mono text-[10px] text-faint">
          verified hash {latest.observed_hash?.slice(0, 16)}… = approved {latest.approved_preview_hash?.slice(0, 16)}…
        </div>
      </div>
      {latest.has_receipt ? (
        <a href={`/api/v1/applications/${id}/receipt`} target="_blank" rel="noreferrer" className="shrink-0 text-xs underline">
          Receipt screenshot
        </a>
      ) : null}
    </div>
  );
}

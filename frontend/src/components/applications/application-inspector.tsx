"use client";

import Link from "next/link";
import { useState } from "react";

import { DeadlineCell } from "@/components/discovery/outcome";
import { EssayScores } from "@/components/vault/essay-synthesizer";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/field";
import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { StatusPill, type Tone } from "@/components/ui/status";
import { cn, formatRelative } from "@/lib/cn";
import { useApplication, useEditDraft, useStartFilling } from "@/lib/m4-queries";
import type { ApplicationDetailT, DraftT } from "@/lib/m4-schemas";

const STARTABLE = new Set(["QUEUED", "RESUME_QUEUED", "BLOCKED_NEEDS_HUMAN"]);

export function stateTone(state: string): Tone {
  if (state === "PAUSED_NEEDS_USER_INPUT" || state === "AWAITING_FINAL_VERIFICATION") return "warn";
  if (state === "SUBMITTED" || state === "SUBMIT_APPROVED") return "ok";
  if (["BLOCKED_NEEDS_HUMAN", "FAILED", "SUBMISSION_FAILED", "EXPIRED"].includes(state)) return "err";
  return "idle";
}

const SOURCE_STYLE: Record<string, string> = {
  PROFILE: "text-fg border-line-strong",
  DOCUMENT: "text-fg border-line-strong",
  ESSAY: "text-fg border-line-strong",
  MEMORY: "text-ok border-ok/40",
  USER: "text-ok border-ok/40",
  NONE: "text-warn border-warn/40",
};

export function ApplicationInspector({ id }: { id: string }) {
  const { data, isLoading, error } = useApplication(id);
  const start = useStartFilling();
  if (error) return <p className="text-xs text-err">{error.message}</p>;
  if (isLoading || !data) return <p className="text-xs text-faint">Loading</p>;
  const { application: a, opportunity: o } = data;
  return (
    <div className="flex flex-col gap-3">
      <Panel
        title={o.title}
        meta={`${o.organization ?? "unknown organization"} · v${a.version}`}
        actions={
          <>
            {STARTABLE.has(a.state) ? (
              <Button size="sm" variant="primary" onClick={() => start.mutate(a.id)} disabled={start.isPending}>
                {a.state === "BLOCKED_NEEDS_HUMAN" ? "Retry filling" : "Start filling"}
              </Button>
            ) : null}
            {data.questions.some((q) => q.status === "OPEN") ? (
              <Button size="sm" asChild>
                <Link href="/inbox">Answer in Inbox</Link>
              </Button>
            ) : null}
          </>
        }
      >
        <div className="grid grid-cols-4 gap-px bg-line">
          <div className="bg-surface px-3 py-2">
            <div className="text-[10px] uppercase tracking-wide text-faint">State</div>
            <StatusPill tone={stateTone(a.state)}>{a.state}</StatusPill>
          </div>
          <div className="bg-surface px-3 py-2">
            <div className="text-[10px] uppercase tracking-wide text-faint">Deadline</div>
            <DeadlineCell iso={a.deadline_at} />
          </div>
          <div className="bg-surface px-3 py-2">
            <div className="text-[10px] uppercase tracking-wide text-faint">AI policy</div>
            <span className={cn("font-mono text-2xs", o.ai_policy === "PROHIBITED" && "text-warn")}>{o.ai_policy}</span>
          </div>
          <div className="min-w-0 bg-surface px-3 py-2">
            <div className="text-[10px] uppercase tracking-wide text-faint">Portal</div>
            {o.portal_url ? (
              <a
                href={o.portal_url}
                target="_blank"
                rel="noopener noreferrer nofollow"
                className="block truncate font-mono text-2xs underline underline-offset-2"
              >
                {o.portal_url}
              </a>
            ) : (
              <span className="font-mono text-2xs text-faint">none</span>
            )}
          </div>
        </div>
        {start.error ? <p className="border-t border-line bg-err-soft px-3 py-1.5 text-xs text-err">{start.error.message}</p> : null}
        {a.state === "AWAITING_FINAL_VERIFICATION" ? (
          <p className="border-t border-line bg-warn-soft px-3 py-1.5 text-xs text-warn">
            Every step is filled and the filler stopped before Submit. Nothing is submitted without your
            recorded OK.
          </p>
        ) : null}
      </Panel>
      <div className="grid grid-cols-5 gap-3">
        <Timeline data={data} />
        <Fields data={data} />
      </div>
      {data.drafts.length ? (
        <Panel title="Essay drafts" meta={`${data.drafts.length}`}>
          <div className="divide-y divide-line">
            {data.drafts.map((d) => (
              <DraftRow key={d.id} draft={d} />
            ))}
          </div>
        </Panel>
      ) : null}
      <Checkpoints data={data} />
    </div>
  );
}

function Timeline({ data }: { data: ApplicationDetailT }) {
  return (
    <Panel title="State timeline" meta={`${data.transitions.length} transitions`} className="col-span-2">
      <ol className="px-3 py-2">
        {data.transitions.map((t, i) => (
          <li key={`${t.at}-${i}`} className="relative flex gap-3 pb-3">
            {i < data.transitions.length - 1 ? (
              <span className="absolute left-[3px] top-3 h-full w-px bg-line" aria-hidden="true" />
            ) : null}
            <span className="mt-1.5 h-[7px] w-[7px] shrink-0 rounded-full bg-line-strong" aria-hidden="true" />
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <StatusPill tone={stateTone(t.to_state)}>{t.to_state}</StatusPill>
                <span className="font-mono text-[10px] text-faint">{t.actor}</span>
              </div>
              <div className="mt-0.5 text-2xs text-muted">{t.reason}</div>
              <div className="font-mono text-[10px] text-faint">{new Date(t.at).toLocaleString()}</div>
            </div>
          </li>
        ))}
        {!data.transitions.length ? <li className="text-xs text-faint">Not started yet.</li> : null}
      </ol>
    </Panel>
  );
}

function Fields({ data }: { data: ApplicationDetailT }) {
  return (
    <Panel title="Fields" meta="value · source · confidence" className="col-span-3">
      <div className="max-h-[520px] overflow-auto">
        <table className="w-full table-fixed border-collapse">
          <thead className="sticky top-0 bg-surface">
            <tr>
              <th className={cn(th, "w-10")}>Step</th>
              <th className={cn(th, "w-48")}>Field</th>
              <th className={th}>Value</th>
              <th className={cn(th, "w-24")}>Source</th>
              <th className={cn(th, "w-14 text-right")}>Conf.</th>
            </tr>
          </thead>
          <tbody>
            {!data.fields.length ? (
              <EmptyRow colSpan={5}>No fields mapped yet.</EmptyRow>
            ) : (
              data.fields.map((f) => (
                <tr key={`${f.step_index}-${f.field_key}`}>
                  <td className={cn(td, "font-mono text-2xs text-faint tabular")}>{f.step_index + 1}</td>
                  <td className={cn(td, "py-1")}>
                    <div className="truncate text-xs" title={f.label}>{f.label}</div>
                    <div className="truncate font-mono text-[10px] text-faint">
                      {f.field_key} · {f.field_type}
                      {f.required ? " · required" : ""}
                    </div>
                  </td>
                  <td className={cn(td, "py-1")}>
                    <div className="line-clamp-2 break-words text-xs" title={f.value ?? undefined}>
                      {f.value ?? <span className="text-faint">{f.required ? "unanswered" : "left blank"}</span>}
                    </div>
                  </td>
                  <td className={td}>
                    <span
                      className={cn(
                        "inline-flex h-5 items-center rounded-xs border px-1.5 font-mono text-[10px]",
                        SOURCE_STYLE[f.source] ?? SOURCE_STYLE.NONE,
                      )}
                    >
                      {f.user_edited ? "USER" : f.source}
                    </span>
                  </td>
                  <td
                    className={cn(
                      td,
                      "text-right font-mono text-2xs tabular",
                      f.confidence < 0.85 ? "text-warn" : "text-muted",
                    )}
                  >
                    {Math.round(f.confidence * 100)}%
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function DraftRow({ draft }: { draft: DraftT }) {
  const edit = useEditDraft();
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState(draft.body);
  return (
    <div className="flex flex-col gap-2 px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-xs font-medium">{draft.prompt}</div>
          <div className="font-mono text-[10px] text-faint">
            {draft.field_key ? `${draft.field_key} · ` : ""}
            {draft.status}
            {draft.model ? ` · ${draft.model}` : ""}
            {draft.word_limit ? ` · limit ${draft.word_limit} words` : ""}
          </div>
        </div>
        <EssayScores draft={draft} />
      </div>
      {draft.requires_manual ? (
        <p className="rounded-sm border border-warn/30 bg-warn-soft px-2 py-1 text-2xs text-warn">
          This portal prohibits AI-generated essays. Write or substantially rewrite this text yourself
          before it can be used.
        </p>
      ) : null}
      {editing ? (
        <>
          <Textarea rows={12} value={body} onChange={(e) => setBody(e.target.value)} />
          <div className="flex justify-end gap-1">
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
            <Button
              size="sm"
              variant="primary"
              disabled={edit.isPending || !body.trim()}
              onClick={() => edit.mutate({ id: draft.id, body }, { onSuccess: () => setEditing(false) })}
            >
              Save edit
            </Button>
          </div>
        </>
      ) : (
        <>
          <p className="whitespace-pre-wrap text-xs leading-relaxed text-muted">{draft.body}</p>
          <div className="flex justify-end">
            <Button size="sm" onClick={() => setEditing(true)}>Edit</Button>
          </div>
        </>
      )}
    </div>
  );
}

function Checkpoints({ data }: { data: ApplicationDetailT }) {
  const [open, setOpen] = useState<string | null>(data.checkpoints.at(-1)?.id ?? null);
  if (!data.checkpoints.length) return null;
  return (
    <Panel title="Checkpoints" meta="encrypted session + fill ledger + screenshot per step">
      <div className="grid grid-cols-5">
        <ul className="col-span-1 border-r border-line">
          {data.checkpoints.map((c) => (
            <li key={c.id}>
              <button
                onClick={() => setOpen(c.id)}
                className={cn(
                  "flex w-full flex-col items-start border-b border-line px-3 py-2 text-left hover:bg-surface-2",
                  open === c.id && "bg-surface-2",
                )}
              >
                <span className="text-xs">#{c.seq} · step {c.step_index + 1}</span>
                <span className="font-mono text-[10px] text-faint">
                  {c.fields_filled} filled · {formatRelative(c.created_at)}
                </span>
              </button>
            </li>
          ))}
        </ul>
        <div className="col-span-4 max-h-[640px] overflow-auto bg-surface-2 p-3">
          {open && data.checkpoints.find((c) => c.id === open)?.has_screenshot ? (
            // eslint-disable-next-line @next/next/no-img-element -- authenticated API image, not optimizable
            <img
              src={`/api/v1/applications/checkpoints/${open}/screenshot`}
              alt="Portal screenshot at this checkpoint"
              className="w-full border border-line"
            />
          ) : (
            <p className="text-xs text-faint">No screenshot for this checkpoint.</p>
          )}
        </div>
      </div>
    </Panel>
  );
}

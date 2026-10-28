"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Field, Input, Textarea } from "@/components/ui/field";
import { Panel } from "@/components/ui/panel";
import { cn, formatRelative } from "@/lib/cn";
import { useDrafts, useSynthesizeDraft } from "@/lib/m4-queries";
import type { DraftT } from "@/lib/m4-schemas";

function Score({ label, value, title }: { label: string; value: number | null; title: string }) {
  const pct = value === null ? null : Math.round(value * 100);
  return (
    <span
      title={title}
      className={cn(
        "inline-flex h-5 items-center gap-1 rounded-xs border px-1.5 font-mono text-[10px] tabular",
        pct === null
          ? "border-line text-faint"
          : pct >= 80
            ? "border-ok/40 text-ok"
            : pct >= 60
              ? "border-warn/40 text-warn"
              : "border-err/40 text-err",
      )}
    >
      {label} {pct === null ? "—" : `${pct}%`}
    </span>
  );
}

export function EssayScores({ draft }: { draft: DraftT }) {
  return (
    <div className="flex shrink-0 gap-1">
      <Score
        label="Voice Match"
        value={draft.voice_match_score}
        title="Cosine similarity between this draft's stylometric features and your fingerprint"
      />
      <Score
        label="Burstiness"
        value={draft.burstiness_score}
        title="1 - |CV_draft - CV_you| / CV_you: how closely sentence-length variation matches yours"
      />
    </div>
  );
}

/** Stage 2: drafts grounded in the student's own essays and profile, scored for voice fidelity. */
export function EssaySynthesizer() {
  const { data: drafts } = useDrafts();
  const synth = useSynthesizeDraft();
  const [prompt, setPrompt] = useState("");
  const [limit, setLimit] = useState("");
  const words = Number(limit);
  return (
    <div className="grid grid-cols-5 gap-3">
      <Panel title="Synthesize a draft" meta="your model · your voice" className="col-span-2">
        <form
          className="flex flex-col gap-3 p-3"
          onSubmit={(e) => {
            e.preventDefault();
            synth.mutate({ prompt: prompt.trim(), word_limit: limit && words >= 50 ? words : null });
          }}
        >
          <Field id="essay-prompt" label="Essay prompt">
            <Textarea
              id="essay-prompt"
              rows={4}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Why do you want to join this program? (500 words)"
            />
          </Field>
          <Field id="essay-limit" label="Word limit" hint="Optional, 50 to 2000.">
            <Input id="essay-limit" inputMode="numeric" value={limit} onChange={(e) => setLimit(e.target.value.replace(/\D/g, ""))} />
          </Field>
          <p className="text-2xs text-faint">
            Retrieves your closest reference passages and profile facts, drafts under your style
            spec, then a critic pass removes clichés (delve, tapestry, testament to, …) and flags
            claims your vault does not support.
          </p>
          <Button type="submit" variant="primary" disabled={prompt.trim().length < 5 || synth.isPending}>
            {synth.isPending ? "Drafting and critiquing" : "Synthesize draft"}
          </Button>
          {synth.error ? <p className="text-xs text-err">{synth.error.message}</p> : null}
        </form>
      </Panel>
      <Panel title="Drafts" meta={drafts ? `${drafts.length}` : undefined} className="col-span-3">
        <div className="max-h-[640px] divide-y divide-line overflow-auto">
          {!drafts?.length ? (
            <p className="px-3 py-6 text-center text-xs text-faint">No drafts yet.</p>
          ) : (
            drafts.map((d) => <DraftItem key={d.id} draft={d} />)
          )}
        </div>
      </Panel>
    </div>
  );
}

function DraftItem({ draft }: { draft: DraftT }) {
  const [open, setOpen] = useState(false);
  const unsupported = Array.isArray(draft.critique.unsupported_claims)
    ? (draft.critique.unsupported_claims as unknown[]).map(String)
    : [];
  const cliches = Array.isArray(draft.critique.cliches) ? (draft.critique.cliches as unknown[]).map(String) : [];
  return (
    <div className="flex flex-col gap-1.5 px-3 py-2">
      <div className="flex items-start justify-between gap-3">
        <button className="min-w-0 text-left" onClick={() => setOpen((v) => !v)}>
          <div className="truncate text-xs font-medium">{draft.prompt}</div>
          <div className="font-mono text-[10px] text-faint">
            {draft.status}
            {draft.model ? ` · ${draft.model}` : ""}
            {draft.created_at ? ` · ${formatRelative(draft.created_at)}` : ""}
            {` · ${draft.body.split(/\s+/).filter(Boolean).length} words`}
          </div>
        </button>
        <EssayScores draft={draft} />
      </div>
      {draft.requires_manual ? (
        <p className="text-2xs text-warn">AI-generated essays are prohibited for this portal: author it yourself.</p>
      ) : null}
      {cliches.length ? <p className="text-2xs text-warn">Clichés remaining: {cliches.join(", ")}</p> : null}
      {unsupported.length ? (
        <p className="text-2xs text-warn">Unsupported by your vault: {unsupported.join("; ")}</p>
      ) : null}
      {open ? <p className="whitespace-pre-wrap text-xs leading-relaxed text-muted">{draft.body}</p> : null}
    </div>
  );
}

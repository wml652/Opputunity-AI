"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/field";
import { Panel } from "@/components/ui/panel";
import { StatusPill } from "@/components/ui/status";
import { ApiError } from "@/lib/api";
import { cn, formatRelative } from "@/lib/cn";
import type { Essay, FingerprintMetrics, FingerprintStatus } from "@/lib/schemas";
import { useDeleteEssay, useEssays, useFingerprint, useSaveEssay } from "@/lib/vault-queries";

const MIN_WORDS = 50;

function countWords(text: string): number {
  return (text.match(/[A-Za-z0-9]+(?:['’][A-Za-z]+)?/g) ?? []).length;
}

export function EssayStudio() {
  const { data: essays, isLoading } = useEssays();
  const { data: status } = useFingerprint();
  const [adding, setAdding] = useState(false);
  const max = status?.max_essays ?? 5;
  const count = essays?.length ?? 0;

  return (
    <div className="grid grid-cols-5 gap-3">
      <div className="col-span-3 flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <div className="flex items-baseline gap-2">
            <h1 className="text-sm font-semibold">Reference essays</h1>
            <span className="font-mono text-2xs text-faint">
              {count}/{max} · recommended {status?.recommended_essays ?? 4}+
            </span>
          </div>
          {count < max && !adding ? (
            <Button size="sm" variant="primary" onClick={() => setAdding(true)}>
              Add essay
            </Button>
          ) : null}
        </div>
        <p className="text-xs text-faint">
          Paste 4–5 essays you wrote yourself. They are chunked and embedded for voice-matched
          retrieval, and every change recomputes a new fingerprint version.
        </p>
        {isLoading ? <p className="font-mono text-xs text-faint">Loading</p> : null}
        {essays?.map((essay, i) => <EssayCard key={essay.id} index={i + 1} essay={essay} />)}
        {adding || (!isLoading && count === 0) ? (
          <EssayCard index={count + 1} essay={null} onDone={() => setAdding(false)} />
        ) : null}
      </div>
      <div className="col-span-2">
        <FingerprintPanel status={status ?? null} />
      </div>
    </div>
  );
}

function EssayCard({
  index,
  essay,
  onDone,
}: {
  index: number;
  essay: Essay | null;
  onDone?: () => void;
}) {
  const save = useSaveEssay();
  const remove = useDeleteEssay();
  const [title, setTitle] = useState(essay?.title ?? "");
  const [context, setContext] = useState(essay?.prompt_context ?? "");
  const [body, setBody] = useState(essay?.body ?? "");
  const [error, setError] = useState<string | null>(null);
  const words = countWords(body);
  const dirty =
    !essay || title !== essay.title || body !== essay.body || context !== (essay.prompt_context ?? "");

  async function onSave() {
    setError(null);
    try {
      await save.mutateAsync({ id: essay?.id, title, body, prompt_context: context || null });
      onDone?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Save failed");
    }
  }

  return (
    <Panel
      title={`Essay ${index}`}
      meta={essay ? `updated ${formatRelative(essay.updated_at)}` : "new"}
      actions={
        <div className="flex items-center gap-1">
          <span
            className={cn("mr-2 font-mono text-2xs tabular", words < MIN_WORDS ? "text-warn" : "text-muted")}
          >
            {words} words
          </span>
          {essay ? (
            <Button
              size="sm"
              variant="danger"
              disabled={remove.isPending}
              onClick={() => window.confirm(`Delete "${essay.title}"?`) && remove.mutate(essay.id)}
            >
              Delete
            </Button>
          ) : onDone ? (
            <Button size="sm" variant="ghost" onClick={onDone}>
              Cancel
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="primary"
            disabled={!dirty || save.isPending || !title.trim() || words < MIN_WORDS}
            onClick={onSave}
          >
            {save.isPending ? "Embedding" : "Save"}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-2 p-3">
        <div className="grid grid-cols-2 gap-2">
          <Input aria-label="Title" placeholder="Title, e.g. Why Computer Science" value={title} onChange={(e) => setTitle(e.target.value)} />
          <Input aria-label="Prompt context" placeholder="Original prompt (optional)" value={context} onChange={(e) => setContext(e.target.value)} />
        </div>
        <Textarea
          aria-label="Essay text"
          rows={essay ? 6 : 10}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          placeholder="Paste the full essay. Keep blank lines between paragraphs: paragraph cadence is part of your fingerprint."
          className="font-serif text-[13px] leading-relaxed"
        />
        {error ? <p className="text-xs text-err">{error}</p> : null}
      </div>
    </Panel>
  );
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex flex-col gap-0.5 bg-surface px-3 py-2" title={hint}>
      <span className="text-2xs uppercase tracking-wide text-faint">{label}</span>
      <span className="font-mono text-base font-medium tabular">{value}</span>
    </div>
  );
}

function Bar({ label, value, max }: { label: string; value: number; max: number }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="grid grid-cols-[88px_1fr_44px] items-center gap-2">
      <span className="truncate text-2xs text-muted">{label}</span>
      <span className="h-1.5 rounded-xs bg-surface-3">
        <span className="block h-1.5 rounded-xs bg-muted" style={{ width: `${pct}%` }} />
      </span>
      <span className="text-right font-mono text-2xs tabular">{value}</span>
    </div>
  );
}

function FingerprintPanel({ status }: { status: FingerprintStatus | null }) {
  const fp = status?.fingerprint ?? null;
  if (!fp) {
    return (
      <Panel title="Stylometric fingerprint">
        <p className="p-3 text-xs text-faint">
          Save at least one reference essay to compute your linguistic fingerprint.
        </p>
      </Panel>
    );
  }
  const m: FingerprintMetrics = fp.metrics;
  const s = m.sentence_length;
  const histMax = Math.max(1, ...s.histogram.map((h) => h.count));
  const punct = Object.entries(m.punctuation_per_1k).sort((a, b) => b[1] - a[1]);
  const punctMax = Math.max(1, ...punct.map(([, v]) => v));
  const enough = (status?.essay_count ?? 0) >= (status?.recommended_essays ?? 4);

  return (
    <div className="sticky top-0 flex flex-col gap-3">
      <Panel
        title="Stylometric fingerprint"
        meta={`v${fp.version} · ${fp.essay_count} essays · ${fp.total_words.toLocaleString()} words`}
        actions={
          <StatusPill tone={enough ? "ok" : "warn"}>{enough ? "Robust" : "Add more essays"}</StatusPill>
        }
      >
        <div className="grid grid-cols-3 gap-px bg-line">
          <Metric label="Burstiness σ/μ" value={s.burstiness.toFixed(3)} hint="Coefficient of variation of sentence length. Human writing is usually 0.4–0.8; uniform LLM prose is lower." />
          <Metric label="Sentence μ ± σ" value={`${s.mean.toFixed(1)} ± ${s.std.toFixed(1)}`} hint="Mean and standard deviation of words per sentence" />
          <Metric label="MTLD" value={m.lexical.mtld.toFixed(1)} hint="Measure of Textual Lexical Diversity (length-robust)" />
          <Metric label="TTR" value={m.lexical.ttr.toFixed(3)} hint="Type-token ratio (depends on length)" />
          <Metric label="Flesch" value={m.readability.flesch_reading_ease.toFixed(0)} hint="Flesch reading ease (higher = easier)" />
          <Metric label="FK grade" value={m.readability.flesch_kincaid_grade.toFixed(1)} hint="Flesch-Kincaid grade level" />
        </div>
      </Panel>

      <Panel title="Sentence length distribution" meta={`range ${s.min}–${s.max} · median ${s.median}`}>
        <div className="flex h-24 items-end gap-1 px-3 pb-1 pt-3">
          {s.histogram.map((h) => (
            <div key={h.bin} className="flex flex-1 flex-col items-center gap-1">
              <span className="w-full rounded-xs bg-muted" style={{ height: `${(h.count / histMax) * 64}px` }} />
            </div>
          ))}
        </div>
        <div className="flex gap-1 px-3 pb-2">
          {s.histogram.map((h) => (
            <span key={h.bin} className="flex-1 text-center font-mono text-[10px] text-faint">
              {h.bin}
            </span>
          ))}
        </div>
      </Panel>

      <Panel title="Top transitions" meta={`${m.transitions.per_1k} per 1k words`}>
        <div className="flex flex-wrap gap-1 p-3">
          {m.transitions.top.length === 0 ? <span className="text-xs text-faint">None detected.</span> : null}
          {m.transitions.top.map((t) => (
            <span key={t.phrase} className="rounded-xs border border-line px-1.5 py-0.5 text-2xs">
              {t.phrase} <span className="font-mono text-faint">×{t.count}</span>
            </span>
          ))}
        </div>
        <div className="border-t border-line px-3 py-2 text-2xs text-faint">
          Sentence openers:{" "}
          {m.transitions.openers.map((o) => `${o.word} (${o.count})`).join(" · ") || "—"}
        </div>
      </Panel>

      <Panel title="Punctuation cadence" meta="per 1,000 words">
        <div className="flex flex-col gap-1.5 p-3">
          {punct.map(([name, value]) => (
            <Bar key={name} label={name.replace("_", " ")} value={value} max={punctMax} />
          ))}
        </div>
        <div className="border-t border-line px-3 py-2 text-2xs text-faint">
          Paragraphs: {m.paragraph_count} · {m.paragraphs.words_mean} words avg ·{" "}
          {m.paragraphs.sentences_mean} sentences avg · {status?.chunk_count ?? 0} embedded chunks
        </div>
      </Panel>
    </div>
  );
}

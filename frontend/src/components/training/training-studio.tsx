"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { cn, formatRelative } from "@/lib/cn";
import { downloadFile, useBackfill, useTrainingOverview, type TrajectoryT } from "@/lib/training-queries";

function Counter({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <div className="flex flex-col gap-1 bg-surface px-3 py-2" title={hint}>
      <span className="text-2xs font-medium uppercase tracking-wide text-faint">{label}</span>
      <span className="font-mono text-xl font-medium tabular">{value}</span>
    </div>
  );
}

function actionText(a: Record<string, unknown> | null): string {
  if (!a) return "";
  const kind = String(a.action ?? "");
  const label = a.label ? ` "${String(a.label)}"` : "";
  const token = a.value_token ? ` <- ${String(a.value_token)}` : "";
  const tier = a.tier ? ` [${String(a.tier)}]` : "";
  return `${kind}${label}${token}${tier}`;
}

function headline(t: TrajectoryT): string {
  const lines = t.dom_signature.split("\n");
  const path = lines.find((l) => l.startsWith("path:"))?.slice(5).trim() ?? "";
  const title = lines.find((l) => l.startsWith("title:"))?.slice(6).trim() ?? "";
  return `${title || "(untitled)"} ${path}`;
}

const SOURCE_TONE: Record<string, string> = {
  teach: "text-ok",
  auto_success: "text-fg",
  auto_failure: "text-err",
  backfill: "text-muted",
};

/**
 * Agent Brain & Training: what the agent learned (DOM state -> symbolic action), the DOM-RAG hit
 * rate, and PII-tokenized SFT / DPO datasets plus the cloud QLoRA kit for fine-tuning.
 */
export function TrainingStudio() {
  const { data, isLoading } = useTrainingOverview();
  const backfill = useBackfill();
  const [error, setError] = useState<string | null>(null);
  const s = data?.stats;

  async function download(path: string, name: string) {
    setError(null);
    try {
      await downloadFile(path, name);
    } catch (e) {
      setError(e instanceof Error ? e.message : "download failed");
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-5 gap-px overflow-hidden rounded-sm border border-line bg-line">
        <Counter label="Learned DOM states" value={s?.total ?? 0} hint="every trajectory: Teach Mode, autonomous runs, backfill" />
        <Counter label="Taught (Teach Mode)" value={s?.by_source.teach ?? 0} />
        <Counter label="SFT action trajectories" value={s?.sft_records ?? 0} />
        <Counter label="DPO correction pairs" value={s?.dpo_pairs ?? 0} hint="your Teach Mode corrections of a failed bot decision" />
        <Counter
          label="Cross-portal RAG hit rate"
          value={`${(s?.rag_hit_rate ?? 0).toFixed(1)}%`}
          hint={`${s?.rag_decisions ?? 0} decisions from recalled demonstrations vs ${s?.llm_decisions ?? 0} model calls`}
        />
      </div>

      <Panel title="Datasets and cloud training" meta="PII-tokenized: {{PROFILE.*}} only">
        <div className="flex flex-wrap items-center gap-2 p-3">
          <Button onClick={() => backfill.mutate()} disabled={backfill.isPending}>
            {backfill.isPending ? "Harvesting" : "Harvest & backfill trajectories from past runs"}
          </Button>
          <Button onClick={() => download("/api/v1/training/export?format=sft", "sft.jsonl")}>
            Download SFT dataset (sft.jsonl)
          </Button>
          <Button onClick={() => download("/api/v1/training/export?format=dpo", "dpo.jsonl")}>
            Download DPO preference dataset (dpo.jsonl)
          </Button>
          <Button onClick={() => download("/api/v1/training/kit?file=notebook", "OOTG_Cloud_Trainer_Colab_Azure.ipynb")}>
            Download Colab / Azure QLoRA notebook (.ipynb)
          </Button>
          <Button variant="ghost" onClick={() => download("/api/v1/training/kit?file=script", "train_qlora_unsloth.py")}>
            CLI script
          </Button>
          {backfill.data ? (
            <span className="font-mono text-2xs text-muted">
              backfilled {backfill.data.from_workflows} from workflows · {backfill.data.from_fields} from past fields
            </span>
          ) : null}
        </div>
        <p className="border-t border-line px-3 py-1.5 text-2xs text-faint">
          Every personal value is replaced by a symbolic token before storage and again at export; a record that still
          contains one is dropped. Train on a free 16 GB Colab / Kaggle T4 (Unsloth 4-bit QLoRA, r=16, alpha=32), then
          serve the result as an OpenAI-compatible endpoint and add it in LLM Gateway as a Custom provider.
        </p>
        {error ? <p className="border-t border-line bg-err-soft px-3 py-1 text-xs text-err">{error}</p> : null}
      </Panel>

      <Panel title="Agent brain: DOM state -> symbolic action" meta={data ? `${data.trajectories.length} most recent` : undefined}>
        <table className="w-full table-fixed border-collapse">
          <thead>
            <tr>
              <th className={cn(th, "w-24")}>Source</th>
              <th className={cn(th, "w-36")}>Portal</th>
              <th className={th}>Page</th>
              <th className={th}>Action</th>
              <th className={cn(th, "w-48")}>Rejected (DPO)</th>
              <th className={cn(th, "w-20")}>When</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <EmptyRow colSpan={6}>Loading</EmptyRow>
            ) : !data?.trajectories.length ? (
              <EmptyRow colSpan={6}>Nothing learned yet. Use Teach Mode on an application, or harvest past runs.</EmptyRow>
            ) : (
              data.trajectories.map((t) => (
                <tr key={t.id} className="hover:bg-surface-2">
                  <td className={cn(td, "font-mono text-2xs", SOURCE_TONE[t.source] ?? "text-muted")}>{t.source}</td>
                  <td className={cn(td, "truncate font-mono text-2xs text-muted")} title={t.domain}>{t.domain}</td>
                  <td className={cn(td, "truncate text-2xs")} title={t.dom_signature}>{headline(t)}</td>
                  <td className={cn(td, "truncate font-mono text-[10px]")} title={actionText(t.chosen_action)}>{actionText(t.chosen_action)}</td>
                  <td className={cn(td, "truncate font-mono text-[10px] text-faint")} title={actionText(t.rejected_action)}>
                    {actionText(t.rejected_action) || "-"}
                  </td>
                  <td className={cn(td, "font-mono text-[10px] text-faint")}>{t.created_at ? formatRelative(t.created_at) : ""}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}

"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/field";
import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { StatusPill } from "@/components/ui/status";
import { ApiError } from "@/lib/api";
import { cn, formatRelative } from "@/lib/cn";
import type { MemoryEntry } from "@/lib/schemas";
import {
  useDeleteMemory,
  useLearnMemory,
  useMemory,
  useRecall,
  useUpdateMemory,
} from "@/lib/vault-queries";

export function MemoryVault() {
  const [search, setSearch] = useState("");
  const { data, isLoading } = useMemory(search.trim());

  return (
    <div className="grid grid-cols-5 gap-3">
      <div className="col-span-3 flex flex-col gap-3">
        <AddMemory />
        <Panel
          title="Learned answers"
          meta={data ? `${data.total} entries` : undefined}
          actions={
            <Input
              aria-label="Search memory"
              placeholder="Search questions and answers"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-6 w-56 text-xs"
            />
          }
        >
          <table className="w-full table-fixed border-collapse">
            <thead>
              <tr>
                <th className={cn(th, "w-[38%]")}>Question</th>
                <th className={th}>Answer</th>
                <th className={cn(th, "w-20")}>Source</th>
                <th className={cn(th, "w-28 text-right")} />
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <EmptyRow colSpan={4}>Loading</EmptyRow>
              ) : !data?.items.length ? (
                <EmptyRow colSpan={4}>
                  {search ? "No matches." : "Nothing learned yet. Answers you give in the Action Inbox land here automatically."}
                </EmptyRow>
              ) : (
                data.items.map((entry) => <MemoryRow key={entry.id} entry={entry} />)
              )}
            </tbody>
          </table>
        </Panel>
      </div>
      <div className="col-span-2">
        <RecallTester />
      </div>
    </div>
  );
}

function AddMemory() {
  const learn = useLearnMemory();
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      await learn.mutateAsync({ question, answer });
      setQuestion("");
      setAnswer("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save");
    }
  }

  return (
    <Panel title="Teach an answer">
      <form onSubmit={onSubmit} className="flex flex-col gap-2 p-3">
        <Input aria-label="Question" placeholder="Portal question, e.g. Why do you want to study abroad?" value={question} onChange={(e) => setQuestion(e.target.value)} />
        <Textarea aria-label="Answer" rows={2} placeholder="Your answer" value={answer} onChange={(e) => setAnswer(e.target.value)} />
        <div className="flex items-center justify-between">
          {error ? <span className="text-xs text-err">{error}</span> : <span className="text-2xs text-faint">The question is embedded (1024-d) for semantic recall.</span>}
          <Button type="submit" variant="primary" size="sm" disabled={learn.isPending || !question.trim() || !answer.trim()}>
            {learn.isPending ? "Embedding" : "Save to memory"}
          </Button>
        </div>
      </form>
    </Panel>
  );
}

function MemoryRow({ entry }: { entry: MemoryEntry }) {
  const update = useUpdateMemory();
  const remove = useDeleteMemory();
  const [editing, setEditing] = useState(false);
  const [question, setQuestion] = useState(entry.question);
  const [answer, setAnswer] = useState(entry.answer);

  if (editing) {
    return (
      <tr className="bg-surface-2">
        <td className={cn(td, "py-2 align-top")}>
          <Textarea aria-label="Question" rows={3} value={question} onChange={(e) => setQuestion(e.target.value)} className="text-xs" />
        </td>
        <td className={cn(td, "py-2 align-top")}>
          <Textarea aria-label="Answer" rows={3} value={answer} onChange={(e) => setAnswer(e.target.value)} className="text-xs" />
        </td>
        <td className={cn(td, "align-top font-mono text-2xs text-faint")}>{entry.source}</td>
        <td className={cn(td, "py-2 text-right align-top")}>
          <div className="flex flex-col items-end gap-1">
            <Button
              size="sm"
              variant="primary"
              disabled={update.isPending}
              onClick={() =>
                update.mutate(
                  {
                    id: entry.id,
                    question: question !== entry.question ? question : undefined,
                    answer: answer !== entry.answer ? answer : undefined,
                  },
                  { onSuccess: () => setEditing(false) },
                )
              }
            >
              Save
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            {update.error ? <span className="text-2xs text-err">{(update.error as Error).message}</span> : null}
          </div>
        </td>
      </tr>
    );
  }
  return (
    <tr className="hover:bg-surface-2">
      <td className={cn(td, "py-1.5 align-top text-xs")}>
        {entry.question}
        <div className="font-mono text-[10px] text-faint">
          {entry.embedding_model} · {formatRelative(entry.updated_at)}
          {entry.times_reused ? ` · reused ${entry.times_reused}×` : ""}
        </div>
      </td>
      <td className={cn(td, "py-1.5 align-top text-xs text-muted")}>
        <span className="line-clamp-3 whitespace-pre-wrap">{entry.answer}</span>
      </td>
      <td className={cn(td, "align-top font-mono text-2xs text-faint")}>{entry.source}</td>
      <td className={cn(td, "py-1.5 text-right align-top")}>
        <div className="flex justify-end gap-1">
          <Button size="sm" onClick={() => setEditing(true)}>
            Edit
          </Button>
          <Button
            size="sm"
            variant="danger"
            disabled={remove.isPending}
            onClick={() => window.confirm("Forget this answer?") && remove.mutate(entry.id)}
          >
            Delete
          </Button>
        </div>
      </td>
    </tr>
  );
}

function SimilarityBar({ value, threshold }: { value: number; threshold: number }) {
  const pass = value >= threshold;
  return (
    <div className="relative h-2 w-full rounded-xs bg-surface-3">
      <span
        className={cn("absolute inset-y-0 left-0 rounded-xs", pass ? "bg-ok" : "bg-warn")}
        style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }}
      />
      <span
        className="absolute -inset-y-0.5 w-px bg-fg"
        style={{ left: `${threshold * 100}%` }}
        title={`auto-fill threshold ${threshold}`}
      />
    </div>
  );
}

function RecallTester() {
  const recall = useRecall();
  const [question, setQuestion] = useState("");
  const result = recall.data;

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (question.trim()) recall.mutate({ question, k: 5 });
  }

  return (
    <Panel title="Semantic recall tester" meta="cosine similarity · threshold 0.85" className="sticky top-0">
      <form onSubmit={onSubmit} className="flex flex-col gap-2 p-3">
        <Textarea
          aria-label="Question to recall"
          rows={2}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Type a question as a portal might phrase it"
        />
        <Button type="submit" variant="primary" size="sm" disabled={recall.isPending || !question.trim()}>
          {recall.isPending ? "Searching" : "Recall"}
        </Button>
      </form>

      {recall.isError ? (
        <p className="border-t border-line bg-err-soft px-3 py-2 text-xs text-err">
          {recall.error instanceof ApiError ? recall.error.message : "Recall failed"}
        </p>
      ) : null}

      {result ? (
        <div className="border-t border-line">
          <div className="flex items-center justify-between px-3 py-2">
            <StatusPill tone={result.auto_fill ? "ok" : "warn"}>
              {result.auto_fill ? "AUTO-FILL" : "ESCALATE TO USER"}
            </StatusPill>
            <span className="font-mono text-2xs text-faint">
              {result.searched} searched{result.stale ? ` · ${result.stale} need re-embedding` : ""}
            </span>
          </div>
          <div className="px-3 pb-1 font-mono text-[10px] text-faint">{result.embedding_model}</div>
          {result.matches.length === 0 ? (
            <p className="px-3 pb-3 text-xs text-faint">No comparable memory entries yet.</p>
          ) : (
            <ol className="flex flex-col divide-y divide-line">
              {result.matches.map((m, i) => (
                <li key={m.memory.id} className={cn("flex flex-col gap-1 px-3 py-2", i === 0 && "bg-surface-2")}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-xs">{m.memory.question}</span>
                    <span className={cn("font-mono text-xs tabular", m.auto_fill ? "text-ok" : "text-warn")}>
                      {m.similarity.toFixed(3)}
                    </span>
                  </div>
                  <SimilarityBar value={m.similarity} threshold={result.threshold} />
                  {i === 0 ? (
                    <p className="line-clamp-2 text-2xs text-muted">{m.memory.answer}</p>
                  ) : null}
                </li>
              ))}
            </ol>
          )}
        </div>
      ) : null}
    </Panel>
  );
}

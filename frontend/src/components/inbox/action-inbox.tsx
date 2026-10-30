"use client";

import Link from "next/link";
import { useState } from "react";

import { DeadlineCell } from "@/components/discovery/outcome";
import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/field";
import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { StatusPill } from "@/components/ui/status";
import { cn, formatRelative } from "@/lib/cn";
import { useAnswerQuestion, useInbox, useStartFilling } from "@/lib/m4-queries";
import type { InboxAppT, InboxQuestionT } from "@/lib/m4-schemas";

import { VerificationDrawer } from "./verification-drawer";

type Tab = "questions" | "ready" | "blocked";

export function ActionInbox() {
  const { data, isLoading } = useInbox();
  const [tab, setTab] = useState<Tab>("questions");
  const counts: Record<Tab, number> = {
    questions: data?.questions.length ?? 0,
    ready: data?.awaiting_verification.length ?? 0,
    blocked: data?.blocked.length ?? 0,
  };
  const tabs: [Tab, string][] = [
    ["questions", "Questions"],
    ["ready", "Ready for OK"],
    ["blocked", "Needs a human"],
  ];
  return (
    <div className="flex flex-col gap-3">
      <div className="flex gap-px self-start overflow-hidden rounded-sm border border-line bg-line" role="tablist">
        {tabs.map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
            className={cn(
              "flex h-7 items-center gap-2 px-3 text-xs",
              tab === key ? "bg-surface-3 font-medium text-fg" : "bg-surface text-muted hover:text-fg",
            )}
          >
            {label}
            <span className={cn("font-mono text-2xs tabular", counts[key] > 0 ? "text-warn" : "text-faint")}>
              {counts[key]}
            </span>
          </button>
        ))}
      </div>
      {isLoading ? (
        <p className="text-xs text-faint">Loading</p>
      ) : tab === "questions" ? (
        <QuestionList questions={data?.questions ?? []} />
      ) : (
        <AppList
          items={tab === "ready" ? (data?.awaiting_verification ?? []) : (data?.blocked ?? [])}
          kind={tab}
        />
      )}
    </div>
  );
}

function QuestionList({ questions }: { questions: InboxQuestionT[] }) {
  if (!questions.length) {
    return (
      <Panel>
        <p className="px-3 py-6 text-center text-xs text-faint">
          No open questions. When a portal asks something the vault and memory cannot answer with at
          least 85% confidence, the filler pauses and the question lands here.
        </p>
      </Panel>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      {questions.map((q) => (
        <QuestionCard key={q.id} q={q} />
      ))}
    </div>
  );
}

function QuestionCard({ q }: { q: InboxQuestionT }) {
  const answer = useAnswerQuestion();
  const [value, setValue] = useState(q.suggested_answer ?? "");
  const [save, setSave] = useState(true);
  const long = q.field_type === "textarea";
  const id = `answer-${q.id}`;
  return (
    <Panel
      title={q.opportunity_title}
      meta={`${q.organization ?? "unknown organization"} · step ${q.step_index + 1} · ${formatRelative(q.created_at)}`}
      actions={
        <Button size="sm" variant="ghost" asChild>
          <Link href={`/applications/${q.application_id}`}>Inspect</Link>
        </Button>
      }
    >
      <form
        className="flex flex-col gap-2 px-3 py-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) answer.mutate({ id: q.id, answer: value.trim(), save_to_memory: save });
        }}
      >
        <label htmlFor={id} className="text-sm font-medium">{q.question}</label>
        <p className="font-mono text-[10px] text-faint">{q.context}</p>
        {q.suggested_answer ? (
          <p className="text-2xs text-muted">
            Suggested from memory:{" "}
            <span className="text-fg">{q.suggested_answer}</span>{" "}
            <span className="font-mono text-warn">({Math.round(q.confidence * 100)}% confidence, below 85%)</span>
          </p>
        ) : (
          <p className="text-2xs text-faint">No similar answer in memory.</p>
        )}
        {q.options.length ? (
          <div className="flex flex-wrap gap-1">
            {q.options.map((o) => (
              <Button key={o} size="sm" variant={value === o ? "primary" : "secondary"} onClick={() => setValue(o)}>
                {o}
              </Button>
            ))}
          </div>
        ) : long ? (
          <Textarea id={id} rows={5} value={value} onChange={(e) => setValue(e.target.value)} />
        ) : (
          <Input id={id} value={value} onChange={(e) => setValue(e.target.value)} placeholder="Your answer" />
        )}
        <div className="flex items-center justify-between gap-3">
          <label className="flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={save} onChange={(e) => setSave(e.target.checked)} />
            Save to Q&amp;A Memory (reused automatically next time)
          </label>
          <Button type="submit" size="sm" variant="primary" disabled={!value.trim() || answer.isPending}>
            {answer.isPending ? "Saving" : "Answer & resume"}
          </Button>
        </div>
        {answer.data ? (
          <p className="text-2xs text-ok">
            {answer.data.resumed
              ? "Saved. The filler is resuming this application."
              : `Saved. ${answer.data.remaining_questions} more question(s) before it resumes.`}
          </p>
        ) : null}
        {answer.error ? <p className="text-2xs text-err">{answer.error.message}</p> : null}
      </form>
    </Panel>
  );
}

function AppList({ items, kind }: { items: InboxAppT[]; kind: "ready" | "blocked" }) {
  const start = useStartFilling();
  const [verifying, setVerifying] = useState<string | null>(null);
  return (
    <Panel
      title={kind === "ready" ? "Awaiting your final verification" : "Blocked: a human must continue"}
      meta={kind === "ready" ? "the filler stopped before Submit" : "CAPTCHA, login wall or diverged replay"}
    >
      <table className="w-full table-fixed border-collapse">
        <thead>
          <tr>
            <th className={th}>Opportunity</th>
            <th className={cn(th, "w-32")}>State</th>
            <th className={cn(th, "w-28")}>Deadline</th>
            <th className={cn(th, "w-40")} />
          </tr>
        </thead>
        <tbody>
          {!items.length ? (
            <EmptyRow colSpan={4}>Nothing here.</EmptyRow>
          ) : (
            items.map(({ application: a, opportunity: o }) => (
              <tr key={a.id} className="hover:bg-surface-2">
                <td className={cn(td, "py-1")}>
                  <div className="truncate text-xs font-medium">{o.title}</div>
                  <div className="truncate font-mono text-[10px] text-faint">{o.organization ?? o.portal_url}</div>
                </td>
                <td className={td}>
                  <StatusPill tone={kind === "ready" ? "ok" : "err"}>{a.state}</StatusPill>
                </td>
                <td className={td}><DeadlineCell iso={a.deadline_at} /></td>
                <td className={cn(td, "text-right")}>
                  <div className="flex justify-end gap-1">
                    {kind === "blocked" ? (
                      <Button size="sm" onClick={() => start.mutate(a.id)} disabled={start.isPending}>Retry</Button>
                    ) : null}
                    {kind === "ready" ? (
                      <Button size="sm" variant="primary" onClick={() => setVerifying(a.id)}>
                        Verify &amp; submit
                      </Button>
                    ) : (
                      <Button size="sm" asChild>
                        <Link href={`/applications/${a.id}`}>Review</Link>
                      </Button>
                    )}
                  </div>
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
      <VerificationDrawer id={verifying} onClose={() => setVerifying(null)} />
    </Panel>
  );
}
